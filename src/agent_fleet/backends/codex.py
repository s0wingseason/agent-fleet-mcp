"""ChatGPT / Codex backend, driven through OpenAI's own `codex app-server` JSON-RPC interface.

One app-server process per agent, so each agent can carry its own MCP identity. Threads are
written to ~/.codex/sessions, so they also show up in the ChatGPT desktop app's Codex mode.
"""

from __future__ import annotations

import asyncio
import json
import logging
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent_fleet import __version__
from agent_fleet.backends.base import (Backend, BackendError, TurnOutcome, TurnRequest,
                                       agent_env, agent_paths, extra_dirs, shim_args)
from agent_fleet.backends.jsonrpc import JsonRpcProcess, RpcError
from agent_fleet.backends.procs import find_binary
from agent_fleet.paths import SERVER_NAME, logs_dir

log = logging.getLogger("agent_fleet.codex")


def _toml(v: Any) -> str:
    """JSON string/array syntax is valid TOML for the values we pass."""
    return json.dumps(v)


def codex_home() -> Path:
    import os
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def user_config_has_server(name: str = SERVER_NAME) -> bool:
    p = codex_home() / "config.toml"
    try:
        data = tomllib.loads(p.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return False
    return name in (data.get("mcp_servers") or {})


@dataclass
class _Active:
    req: TurnRequest
    future: asyncio.Future
    codex_turn_id: str | None = None
    texts: list[str] = field(default_factory=list)
    final: str | None = None


class _Session:
    def __init__(self, backend: "CodexBackend", agent: dict[str, Any]):
        self.backend = backend
        self.agent_id = agent["id"]
        self.thread_id: str | None = agent.get("session_id")
        self.rpc: JsonRpcProcess | None = None
        self.active: _Active | None = None

    def on_notification(self, method: str, p: dict[str, Any]) -> None:
        a = self.active
        if a is None or (p.get("threadId") and p.get("threadId") != self.thread_id):
            return
        emit = a.req.emit
        if method == "turn/started":
            a.codex_turn_id = (p.get("turn") or {}).get("id")
        elif method == "item/completed":
            item = p.get("item") or {}
            t = item.get("type")
            if t == "agentMessage":
                text = item.get("text") or ""
                a.texts.append(text)
                if item.get("phase") == "final_answer":
                    a.final = text
                emit("assistant", text, {"phase": item.get("phase")})
            elif t == "commandExecution":
                out = (item.get("aggregatedOutput") or "")[-4000:]
                emit("tool", f"$ {item.get('command')}  (exit {item.get('exitCode')})", {"output": out})
            elif t == "fileChange":
                paths = [c.get("path") for c in item.get("changes") or [] if isinstance(c, dict)]
                emit("tool", "edited: " + ", ".join(str(x) for x in paths if x), None)
            elif t == "mcpToolCall":
                emit("tool", f"{item.get('server')}.{item.get('tool')} -> {item.get('status')}", None)
            elif t == "plan":
                emit("plan", item.get("text") or "", None)
            elif t in ("reasoning", "userMessage", "hookPrompt"):
                pass
            elif t:
                emit("tool", t, None)
        elif method == "error":
            err = (p.get("error") or {}).get("message") or "error"
            if not p.get("willRetry"):
                emit("error", err, None)
        elif method == "turn/completed":
            turn = p.get("turn") or {}
            status = turn.get("status") or "completed"
            err = (turn.get("error") or {}).get("message") if turn.get("error") else None
            output = a.final if a.final is not None else (a.texts[-1] if a.texts else "")
            mapped = {"completed": "completed", "interrupted": "interrupted"}.get(status, "failed")
            if not a.future.done():
                a.future.set_result(TurnOutcome(mapped, output, err, self.thread_id))

    async def on_request(self, method: str, p: dict[str, Any]) -> dict[str, Any] | None:
        auto = bool(self.backend.cfg.get("auto_approve"))
        if method in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval"):
            if self.active:
                self.active.req.emit("approval", f"{method}: {'accepted' if auto else 'declined'}", None)
            return {"decision": "accept" if auto else "decline"}
        if method == "item/tool/requestUserInput":
            return {"answers": {}}
        if method == "mcpServer/elicitation/request":
            return {"action": "decline", "content": None, "_meta": None}
        return None


class CodexBackend(Backend):
    type_name = "codex"
    label = "ChatGPT / Codex (codex app-server)"

    def __init__(self, name: str, cfg: dict[str, Any]):
        super().__init__(name, cfg)
        self.sessions: dict[str, _Session] = {}

    def _binary(self) -> str:
        found = find_binary("codex", self.cfg.get("binary", ""))
        if not found:
            raise BackendError("Codex CLI not found. Install it with `npm i -g @openai/codex` "
                               "(the ChatGPT desktop app shares its sign-in).")
        return found[0]

    async def probe(self) -> dict[str, Any]:
        found = find_binary("codex", self.cfg.get("binary", ""))
        if not found:
            return {"available": False, "version": "", "detail": "Codex CLI not found (npm i -g @openai/codex)"}
        signed_in = (codex_home() / "auth.json").exists()
        return {"available": signed_in, "version": found[1], "path": found[0],
                "detail": "ready" if signed_in else "not signed in: run `codex login` or sign in to the ChatGPT app"}

    def _argv(self, agent: dict[str, Any]) -> list[str]:
        argv = [self._binary(), "app-server"]
        over: list[str] = []
        if agent["role"] in ("manager", "lead"):
            cmd = shim_args("codex", agent["id"])
            over += [f"mcp_servers.{SERVER_NAME}.command={_toml(cmd[0])}",
                     f"mcp_servers.{SERVER_NAME}.args={_toml(cmd[1:])}",
                     f"mcp_servers.{SERVER_NAME}.enabled=true",
                     f"mcp_servers.{SERVER_NAME}.tool_timeout_sec=120"]
        elif user_config_has_server():
            over.append(f"mcp_servers.{SERVER_NAME}.enabled=false")  # children don't direct others
        dirs = extra_dirs(agent)
        if dirs:
            over.append(f"sandbox_workspace_write.writable_roots={_toml(dirs)}")
        for o in over:
            argv += ["-c", o]
        return argv

    async def _ensure(self, agent: dict[str, Any]) -> _Session:
        s = self.sessions.get(agent["id"])
        if s and s.rpc and s.rpc.alive and s.thread_id:
            return s
        s = s or _Session(self, agent)
        workdir, _, _ = agent_paths(agent)
        s.rpc = JsonRpcProcess(self._argv(agent), cwd=str(workdir), env=agent_env(agent),
                               on_notification=s.on_notification, on_request=s.on_request,
                               stderr_path=str(logs_dir() / f"codex-{agent['id']}.log"))
        await s.rpc.start()
        await s.rpc.request("initialize", {
            "clientInfo": {"name": "agent_fleet", "title": "agent-fleet", "version": __version__},
            "capabilities": {"experimentalApi": False, "requestAttestation": False,
                             "optOutNotificationMethods": ["item/agentMessage/delta",
                                                           "item/commandExecution/outputDelta",
                                                           "item/reasoning/textDelta",
                                                           "item/reasoning/summaryTextDelta"]}},
            timeout=60)
        await s.rpc.notify("initialized")
        common = {"cwd": str(workdir), "approvalPolicy": self.cfg.get("approval", "never"),
                  "sandbox": self.cfg.get("sandbox", "workspace-write"),
                  "developerInstructions": agent.get("instructions") or None}
        if agent.get("model") or self.cfg.get("model"):
            common["model"] = agent.get("model") or self.cfg.get("model")
        if s.thread_id:
            res = await s.rpc.request("thread/resume", {"threadId": s.thread_id, "excludeTurns": True, **common}, 90)
        else:
            res = await s.rpc.request("thread/start", {**common, "ephemeral": not self.cfg.get("visible_in_app", True)}, 90)
            s.thread_id = res["thread"]["id"]
            try:
                await s.rpc.request("thread/name/set", {"threadId": s.thread_id,
                                                        "name": f"[fleet] {agent['name']}"}, 15)
            except (RpcError, asyncio.TimeoutError):
                pass
        self.sessions[agent["id"]] = s
        return s

    async def start(self, agent: dict[str, Any]) -> dict[str, Any]:
        s = await self._ensure(agent)
        return {"session_id": s.thread_id}

    async def run_turn(self, req: TurnRequest) -> TurnOutcome:
        s = await self._ensure(req.agent)
        fut = asyncio.get_running_loop().create_future()
        s.active = _Active(req=req, future=fut)
        try:
            await s.rpc.request("turn/start", {"threadId": s.thread_id, "input": [
                {"type": "text", "text": req.text, "text_elements": []}]}, 60)
            return await asyncio.wait_for(fut, req.timeout)
        except asyncio.TimeoutError:
            await self.interrupt(req.agent)
            return TurnOutcome("failed", "", f"timed out after {int(req.timeout)}s", s.thread_id)
        except RpcError as e:
            return TurnOutcome("failed", "", f"codex: {e}", s.thread_id)
        finally:
            s.active = None

    async def interrupt(self, agent: dict[str, Any]) -> bool:
        s = self.sessions.get(agent["id"])
        if not s or not s.active or not s.active.codex_turn_id or not s.rpc:
            return False
        try:
            await s.rpc.request("turn/interrupt", {"threadId": s.thread_id,
                                                   "turnId": s.active.codex_turn_id}, 15)
            return True
        except (RpcError, asyncio.TimeoutError):
            return False

    async def stop(self, agent: dict[str, Any]) -> None:
        s = self.sessions.pop(agent["id"], None)
        if s and s.rpc:
            await s.rpc.close()

    async def shutdown(self) -> None:
        for aid in list(self.sessions):
            s = self.sessions.pop(aid)
            if s.rpc:
                await s.rpc.close()

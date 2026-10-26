"""Claude Code backend via `claude -p --output-format stream-json` (prompt sent on stdin)."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from agent_fleet.backends.base import (Backend, BackendError, TurnOutcome, TurnRequest,
                                       agent_env, agent_paths, extra_dirs, shim_args)
from agent_fleet.backends.procs import NO_WINDOW, find_binary
from agent_fleet.paths import SERVER_NAME


class ClaudeCodeBackend(Backend):
    type_name = "claude-code"
    label = "Claude Code (claude -p)"

    def __init__(self, name: str, cfg: dict[str, Any]):
        super().__init__(name, cfg)
        self.running: dict[str, asyncio.subprocess.Process] = {}
        self.stopped: set[str] = set()

    async def probe(self) -> dict[str, Any]:
        found = find_binary("claude", self.cfg.get("binary", ""))
        if not found:
            return {"available": False, "version": "", "detail": "Claude Code CLI not found"}
        return {"available": True, "version": found[1], "detail": "ready"}

    async def run_turn(self, req: TurnRequest) -> TurnOutcome:
        found = find_binary("claude", self.cfg.get("binary", ""))
        if not found:
            raise BackendError("Claude Code CLI (claude) not found.")
        a = req.agent
        workdir, _, _ = agent_paths(a)
        argv = [found[0], "-p", "--output-format", "stream-json", "--verbose",
                "--permission-mode", self.cfg.get("permission_mode", "acceptEdits")]
        if a.get("session_id"):
            argv += ["--resume", a["session_id"]]
        model = a.get("model") or self.cfg.get("model")
        if model:
            argv += ["--model", model]
        if a.get("instructions"):
            argv += ["--append-system-prompt", a["instructions"]]
        for d in extra_dirs(a):
            argv += ["--add-dir", d]
        if a["role"] == "manager":
            cmd = shim_args("claude-code", a["id"])
            argv += ["--mcp-config", json.dumps({"mcpServers": {SERVER_NAME: {"command": cmd[0], "args": cmd[1:]}}})]
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=str(workdir), env=agent_env(a), stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            limit=32 * 1024 * 1024, creationflags=NO_WINDOW)
        self.running[a["id"]] = proc
        session, result = a.get("session_id"), None

        async def pump() -> None:
            nonlocal session, result
            assert proc.stdin and proc.stdout
            proc.stdin.write(req.text.encode("utf-8"))
            await proc.stdin.drain()
            proc.stdin.close()
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                t = ev.get("type")
                if t == "system" and ev.get("subtype") == "init":
                    session = ev.get("session_id") or session
                elif t == "assistant":
                    for b in (ev.get("message") or {}).get("content") or []:
                        if b.get("type") == "text" and b.get("text", "").strip():
                            req.emit("assistant", b["text"], None)
                        elif b.get("type") == "tool_use":
                            req.emit("tool", str(b.get("name")), {"input": json.dumps(b.get("input"))[:2000]})
                elif t == "result":
                    result = ev

        stderr = ""
        try:
            await asyncio.wait_for(pump(), req.timeout)
            if proc.stderr:
                stderr = (await proc.stderr.read()).decode("utf-8", "replace")
            await proc.wait()
        except asyncio.TimeoutError:
            proc.kill()
            return TurnOutcome("failed", "", f"timed out after {int(req.timeout)}s", session)
        finally:
            self.running.pop(a["id"], None)
            was_stopped = a["id"] in self.stopped
            self.stopped.discard(a["id"])
        if result is None:
            if was_stopped:
                return TurnOutcome("interrupted", "", None, session)
            return TurnOutcome("failed", "", f"claude exited ({proc.returncode}) without a result. {stderr[-1500:]}".strip(), session)
        session = result.get("session_id") or session
        if result.get("is_error"):
            return TurnOutcome("failed", "", str(result.get("result") or result.get("subtype")), session)
        return TurnOutcome("completed", str(result.get("result") or ""), None, session)

    async def interrupt(self, agent: dict[str, Any]) -> bool:
        proc = self.running.get(agent["id"])
        if proc and proc.returncode is None:
            self.stopped.add(agent["id"])
            proc.terminate()
            return True
        return False

    async def stop(self, agent: dict[str, Any]) -> None:
        await self.interrupt(agent)

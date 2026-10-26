"""Google Antigravity backend via the official `agy` CLI in print mode (stream-json).

Each turn runs `agy -p ... --output-format stream-json`, resuming the same conversation by ID,
so the conversation also shows up in Antigravity's own history.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from agent_fleet.backends.base import (Backend, BackendError, TurnOutcome, TurnRequest,
                                       agent_env, agent_paths, extra_dirs)
from agent_fleet.backends.procs import NO_WINDOW, find_binary

ARG_PROMPT_LIMIT = 6000  # Windows caps a whole command line at 32,767 characters


class StreamParser:
    """Incremental parser for agy stream-json output."""

    def __init__(self) -> None:
        self.conversation_id: str | None = None
        self.steps: dict[Any, dict[str, Any]] = {}
        self.result: dict[str, Any] | None = None

    def feed(self, raw: str) -> list[tuple[str, str, dict | None]]:
        try:
            ev = json.loads(raw)
        except json.JSONDecodeError:
            return []
        out: list[tuple[str, str, dict | None]] = []
        kind = ev.get("event")
        if kind == "init":
            self.conversation_id = ev.get("conversation_id") or self.conversation_id
        elif kind == "step_update":
            su = ev.get("step_update") or {}
            self.conversation_id = self.conversation_id or su.get("conversation_id")
            step = self.steps.setdefault(su.get("step_index"), {"type": su.get("step_type"), "text": "", "done": False})
            step["type"] = su.get("step_type") or step["type"]
            step["text"] += su.get("text_delta") or ""
            if su.get("state") == "DONE" and not step["done"]:
                step["done"] = True
                if step["type"] == "agent_response":
                    if step["text"].strip():
                        out.append(("assistant", step["text"].strip(), None))
                elif step["type"] not in ("user_input", "unknown", None):
                    out.append(("tool", str(step["type"]), None))
        elif kind == "result":
            self.result = ev.get("result") or {}
            self.conversation_id = self.conversation_id or self.result.get("conversation_id")
        return out


class AntigravityBackend(Backend):
    type_name = "antigravity"
    label = "Google Antigravity (agy CLI)"

    def __init__(self, name: str, cfg: dict[str, Any]):
        super().__init__(name, cfg)
        self.running: dict[str, asyncio.subprocess.Process] = {}
        self.stopped: set[str] = set()

    async def probe(self) -> dict[str, Any]:
        found = find_binary("agy", self.cfg.get("binary", ""))
        if not found:
            return {"available": False, "version": "", "detail": "Antigravity CLI (agy) not found"}
        return {"available": True, "version": found[1], "detail": "ready"}

    def _argv(self, req: TurnRequest, prompt: str) -> list[str]:
        found = find_binary("agy", self.cfg.get("binary", ""))
        if not found:
            raise BackendError("Antigravity CLI (agy) not found. Install it from antigravity.google/cli.")
        a = req.agent
        argv = [found[0], "-p", prompt, "--output-format", "stream-json",
                "--print-timeout", f"{int(req.timeout)}s"]
        if a.get("session_id"):
            argv += ["--conversation", a["session_id"]]
        model = a.get("model") or self.cfg.get("model")
        if model:
            argv += ["--model", model]
        if self.cfg.get("mode"):
            argv += ["--mode", self.cfg["mode"]]
        if self.cfg.get("sandbox"):
            argv.append("--sandbox")
        if self.cfg.get("skip_permissions"):
            argv.append("--dangerously-skip-permissions")
        for d in extra_dirs(a):
            argv += ["--add-dir", d]
        return argv

    async def run_turn(self, req: TurnRequest) -> TurnOutcome:
        aid = req.agent["id"]
        workdir, _, _ = agent_paths(req.agent)
        prompt = req.text
        if req.first_turn and req.agent.get("instructions"):
            prompt = (f"<agent-fleet-instructions>\n{req.agent['instructions']}\n"
                      f"</agent-fleet-instructions>\n\n{prompt}")
        if len(prompt) > ARG_PROMPT_LIMIT:
            spill = workdir / f".fleet-prompt-{req.turn_id}.md"
            spill.write_text(prompt, encoding="utf-8")
            prompt = f"Your full instructions for this turn are in the file {spill}. Read it now and follow it exactly."
        proc = await asyncio.create_subprocess_exec(
            *self._argv(req, prompt), cwd=str(workdir), env=agent_env(req.agent),
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, limit=32 * 1024 * 1024, creationflags=NO_WINDOW)
        self.running[aid] = proc
        parser = StreamParser()

        async def pump() -> None:
            assert proc.stdout
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                for kind, text, data in parser.feed(line.decode("utf-8", "replace")):
                    req.emit(kind, text, data)

        stderr = ""
        was_stopped = False
        try:
            await asyncio.wait_for(pump(), req.timeout + 30)
            if proc.stderr:
                stderr = (await proc.stderr.read()).decode("utf-8", "replace")
            await proc.wait()
        except asyncio.TimeoutError:
            proc.kill()
            return TurnOutcome("failed", "", f"timed out after {int(req.timeout)}s", parser.conversation_id)
        finally:
            self.running.pop(aid, None)
            was_stopped = aid in self.stopped
            self.stopped.discard(aid)
        res, conv = parser.result, parser.conversation_id
        if res is None:
            if was_stopped:
                return TurnOutcome("interrupted", "", None, conv)
            return TurnOutcome("failed", "", f"agy exited ({proc.returncode}) without a result. {stderr[-1500:]}".strip(), conv)
        ok = str(res.get("status", "")).upper() == "SUCCESS"
        return TurnOutcome("completed" if ok else "failed", (res.get("response") or "").strip(),
                           None if ok else f"agy status {res.get('status')}", conv)

    async def interrupt(self, agent: dict[str, Any]) -> bool:
        proc = self.running.get(agent["id"])
        if proc and proc.returncode is None:
            self.stopped.add(agent["id"])
            proc.terminate()
            return True
        return False

    async def stop(self, agent: dict[str, Any]) -> None:
        await self.interrupt(agent)

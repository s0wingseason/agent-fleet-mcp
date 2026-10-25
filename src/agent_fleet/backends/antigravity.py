"""Google Antigravity backend, driven through the official `agy` CLI in print mode.

Each turn runs `agy -p ... --output-format stream-json`, resuming the same conversation by ID,
so the conversation is also visible in Antigravity's own history.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from agent_fleet.backends.base import (Backend, BackendError, TurnOutcome, TurnRequest,
                                       agent_env, agent_paths, extra_dirs)
from agent_fleet.backends.procs import NO_WINDOW, find_binary

log = logging.getLogger("agent_fleet.antigravity")

ARG_PROMPT_LIMIT = 6000  # Windows caps a whole command line at 32,767 characters


def build_prompt(req: TurnRequest) -> str:
    text = req.text
    if req.first_turn and req.agent.get("instructions"):
        text = (f"<agent-fleet-instructions>\n{req.agent['instructions']}\n"
                f"</agent-fleet-instructions>\n\n{text}")
    return text


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
            idx = su.get("step_index")
            step = self.steps.setdefault(idx, {"type": su.get("step_type"), "text": "", "done": False})
            step["type"] = su.get("step_type") or step["type"]
            step["text"] += su.get("text_delta") or ""
            if su.get("state") == "DONE" and not step["done"]:
                step["done"] = True
                if step["type"] == "agent_response":
                    if step["text"].strip():
                        out.append(("assistant", step["text"].strip(), None))
                elif step["type"] not in ("user_input", "unknown", None):
                    extra = {k: v for k, v in su.items() if k not in
                             ("conversation_id", "step_index", "state", "text_delta", "usage")}
                    out.append(("tool", str(step["type"]), extra or None))
        elif kind == "result":
            self.result = ev.get("result") or {}
            self.conversation_id = self.conversation_id or self.result.get("conversation_id")
        return out


def parse_stream(lines: list[str]) -> dict[str, Any]:
    """Parse a complete agy stream-json transcript (used by tests and diagnostics)."""
    p = StreamParser()
    events: list[tuple[str, str, dict | None]] = []
    for line in lines:
        events += p.feed(line)
    return {"conversation_id": p.conversation_id, "events": events, "result": p.result}


class AntigravityBackend(Backend):
    type_name = "antigravity"
    label = "Google Antigravity (agy CLI)"

    def __init__(self, name: str, cfg: dict[str, Any]):
        super().__init__(name, cfg)
        self.running: dict[str, asyncio.subprocess.Process] = {}

    async def probe(self) -> dict[str, Any]:
        found = find_binary("agy", self.cfg.get("binary", ""))
        if not found:
            return {"available": False, "version": "", "detail": "Antigravity CLI (agy) not found"}
        return {"available": True, "version": found[1], "path": found[0], "detail": "ready"}

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
        workdir, _, _ = agent_paths(req.agent)
        prompt = build_prompt(req)
        if len(prompt) > ARG_PROMPT_LIMIT:
            spill = workdir / f".fleet-prompt-{req.turn_id}.md"
            spill.write_text(prompt, encoding="utf-8")
            prompt = (f"Your full instructions for this turn are in the file {spill}. "
                      "Read that file now and follow it exactly.")
        proc = await asyncio.create_subprocess_exec(
            *self._argv(req, prompt), cwd=str(workdir), env=agent_env(req.agent),
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, limit=32 * 1024 * 1024, creationflags=NO_WINDOW)
        self.running[req.agent["id"]] = proc
        lines: list[str] = []
        emitted = 0

        async def pump() -> None:
            nonlocal emitted
            assert proc.stdout
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                lines.append(line.decode("utf-8", "replace"))
                st = parse_stream(lines)
                for kind, text, data in st["events"][emitted:]:
                    req.emit(kind, text, data)
                emitted = len(st["events"])

        try:
            await asyncio.wait_for(pump(), req.timeout + 30)
            stderr = (await proc.stderr.read()).decode("utf-8", "replace") if proc.stderr else ""
            await proc.wait()
        except asyncio.TimeoutError:
            proc.kill()
            return TurnOutcome("failed", "", f"timed out after {int(req.timeout)}s",
                               parse_stream(lines)["conversation_id"])
        finally:
            self.running.pop(req.agent["id"], None)
        st = parse_stream(lines)
        res = st["result"]
        conv = st["conversation_id"]
        if res is None:
            if proc.returncode not in (0, None) and proc.returncode < 0 or proc.returncode == 1:
                pass
            return TurnOutcome("interrupted" if proc.returncode and proc.returncode < 0 else "failed",
                               "", f"agy exited ({proc.returncode}) without a result. {stderr[-1500:]}".strip(), conv)
        ok = str(res.get("status", "")).upper() == "SUCCESS"
        return TurnOutcome("completed" if ok else "failed", (res.get("response") or "").strip(),
                           None if ok else f"agy status {res.get('status')}: {res.get('error') or ''}".strip(),
                           conv, {"usage": res.get("usage")})

    async def interrupt(self, agent: dict[str, Any]) -> bool:
        proc = self.running.get(agent["id"])
        if proc and proc.returncode is None:
            proc.terminate()
            return True
        return False

    async def stop(self, agent: dict[str, Any]) -> None:
        await self.interrupt(agent)

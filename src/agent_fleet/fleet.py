"""Core: agent registry, lead/manager/child permissions, turn queues, file exchange.

Each MCP host process (Claude Desktop, ChatGPT/Codex, Antigravity, ...) runs its own agent-fleet
server. Agents run inside the process that created them; all processes share one SQLite file,
so every app can see every agent, its status and its transcript.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
import uuid
from pathlib import Path
from typing import Any

import psutil

from agent_fleet import native
from agent_fleet.backends import make_backend
from agent_fleet.backends.base import BackendError, TurnOutcome, TurnRequest, agent_meta
from agent_fleet.config import load_config
from agent_fleet.paths import agents_dir, db_path, exchange_dir, fleet_home
from agent_fleet.security import check_path, expand_roots
from agent_fleet.store import Store

APP_BACKEND = {"codex": "codex", "claude-code": "claude-code", "antigravity": "antigravity"}


class FleetError(Exception):
    pass


def preamble(role: str, name: str, work: Path, inbox: Path, outbox: Path, max_children: int) -> str:
    common = (f"Your working folder is {work}. Files sent to you arrive in {inbox}. "
              f"Save deliverables in {outbox}. End each reply with a short summary of what you did "
              "and the paths of any files you produced.")
    if role == "manager":
        return (f"You are '{name}', a MANAGER agent in agent-fleet, working for a lead agent. "
                f"You can create and direct up to {max_children} child agents with the agent-fleet "
                "MCP tools (spawn_agent, send_message, wait_for_agents, get_transcript, send_files, "
                "fetch_files). Split work across children when it helps, check their results, and "
                "report back. " + common)
    return (f"You are '{name}', a CHILD agent in agent-fleet. Do the task you are given; you cannot "
            "create other agents. " + common)


def _short(text: str | None, n: int) -> str | None:
    if text is None or len(text) <= n:
        return text
    return text[:n] + f"\n...[{len(text) - n} more characters; use get_transcript]"


class Fleet:
    def __init__(self, host: str = "unknown", agent_id: str | None = None, cfg: dict | None = None):
        self.cfg = cfg or load_config()
        self.host = host
        self.store = Store(db_path())
        self.me = self.store.get_agent(agent_id) if agent_id else None
        self.backends = {n: make_backend(n, b) for n, b in self.cfg["backends"].items() if b.get("enabled", True)}
        self.queues: dict[str, asyncio.Queue] = {}
        self.workers: dict[str, asyncio.Task] = {}
        p = psutil.Process()
        self.key = [p.pid, round(p.create_time(), 2)]
        lim = self.cfg["limits"]
        self.max_live = int(lim["max_live_agents"])
        self.max_children = int(lim["max_children_per_manager"])
        self.turn_timeout = float(lim["turn_timeout_seconds"])
        self.max_wait = float(lim["max_wait_seconds"])

    # -- identity & permissions -------------------------------------------------------------
    @property
    def role(self) -> str:
        return self.me["role"] if self.me else "lead"

    def _get(self, agent_id: str) -> dict[str, Any]:
        a = self.store.get_agent(agent_id)
        if not a:
            raise FleetError(f"No agent with id {agent_id}")
        return a

    def _check_control(self, a: dict) -> None:
        if self.role == "lead":
            return
        if self.role == "manager" and a["parent_id"] == self.me["id"]:
            return
        raise FleetError("Managers can only direct their own child agents." if self.role == "manager"
                         else "Child agents can't direct other agents.")

    def _check_read(self, a: dict) -> None:
        if self.me and a["id"] == self.me["id"]:
            return
        self._check_control(a)

    def _require_lead(self) -> None:
        if self.role != "lead":
            raise FleetError("Only the lead agent can do this.")

    @staticmethod
    def _alive(owner: list | None) -> bool:
        if not owner:
            return False
        try:
            return round(psutil.Process(owner[0]).create_time(), 2) == owner[1]
        except psutil.Error:
            return False

    def _own(self, a: dict) -> dict:
        """Make sure this process runs `a`; take it over if its previous process has exited."""
        meta = agent_meta(a)
        if meta.get("owner") == self.key:
            return a
        if self._alive(meta.get("owner")):
            raise FleetError(f"Agent '{a['name']}' is run from another app's agent-fleet process "
                             f"({meta.get('host')}, pid {meta['owner'][0]}). You can read its status, "
                             "transcript and files here; send it messages from that app.")
        meta.update(owner=self.key, host=self.host)
        self.store.update_agent(a["id"], meta=json.dumps(meta),
                                status="stopped" if a["status"] == "stopped" else "idle")
        with self.store._lock:
            self.store.db.execute("update turns set status='failed', error='its app closed before it finished', "
                                  "finished_at=? where agent_id=? and status in ('queued','running')",
                                  (time.time(), a["id"]))
        return self._get(a["id"])

    # -- views ------------------------------------------------------------------------------
    def _turn_view(self, t: dict | None, n: int = 12000) -> dict | None:
        if not t:
            return None
        return {"turn_id": t["id"], "seq": t["seq"], "status": t["status"],
                "output": _short(t["output"], n), "error": t["error"]}

    def _agent_view(self, a: dict, n: int = 12000) -> dict:
        meta = agent_meta(a)
        status = a["status"]
        if status == "running" and meta.get("owner") != self.key and not self._alive(meta.get("owner")):
            status = "orphaned"
        return {"agent_id": a["id"], "name": a["name"], "role": a["role"], "backend": a["backend"],
                "parent_id": a["parent_id"], "status": status, "session_id": a["session_id"],
                "workdir": a["workdir"], "inbox": meta.get("inbox"), "outbox": meta.get("outbox"),
                "app": meta.get("host"), "last_error": a["last_error"],
                "latest_turn": self._turn_view(self.store.latest_turn(a["id"]), n)}

    def _visible(self) -> list[dict]:
        agents = self.store.list_agents()
        if self.role == "lead":
            return agents
        if self.role == "manager":
            return [a for a in agents if a["id"] == self.me["id"] or a["parent_id"] == self.me["id"]]
        return [a for a in agents if a["id"] == self.me["id"]]

    # -- tools ------------------------------------------------------------------------------
    async def status(self) -> dict:
        names = list(self.backends)
        probes = await asyncio.gather(*(self.backends[n].probe() for n in names), return_exceptions=True)
        backends = {n: (p if isinstance(p, dict) else {"available": False, "detail": str(p)})
                    for n, p in zip(names, probes)}
        you = {"role": self.role, "app": self.host}
        if self.me:
            you.update(agent_id=self.me["id"], name=self.me["name"])
        return {"you": you, "backends": backends, "fleet_folder": str(fleet_home()),
                "exchange_folder": str(exchange_dir()),
                "agents": [self._agent_view(a, 400) for a in self._visible()]}

    async def spawn(self, name: str, backend: str, role: str = "manager", task: str = "",
                    instructions: str = "", parent_id: str = "", model: str = "", workdir: str = "",
                    session_id: str | None = None) -> dict:
        if self.role == "child":
            raise FleetError("Child agents can't create agents.")
        if self.role == "manager":
            role, parent_id = "child", self.me["id"]
            if len(self.store.children_of(parent_id)) >= self.max_children:
                raise FleetError(f"You already have {self.max_children} live children; stop one first.")
            if workdir:
                raise FleetError("Only the lead can choose a working folder.")
        if role not in ("manager", "child"):
            raise FleetError("role must be 'manager' or 'child'.")
        if parent_id and self._get(parent_id)["role"] != "manager":
            raise FleetError("parent_id must be a manager agent.")
        if self.store.count_live() >= self.max_live:
            raise FleetError(f"The fleet already has {self.max_live} live agents; stop some first.")
        if backend not in self.backends:
            raise FleetError(f"Unknown or disabled backend '{backend}'. Enabled: {', '.join(self.backends)}")
        aid = "ag-" + uuid.uuid4().hex[:10]
        base = agents_dir() / aid
        inbox, outbox = base / "inbox", base / "outbox"
        work = Path(os.path.expandvars(workdir)).expanduser() if workdir else base / "work"
        for p in (work, inbox, outbox):
            p.mkdir(parents=True, exist_ok=True)
        text = preamble(role, name, work, inbox, outbox, self.max_children)
        if instructions:
            text += "\n\n" + instructions
        meta = {"inbox": str(inbox), "outbox": str(outbox), "owner": self.key, "host": self.host}
        self.store.add_agent({
            "id": aid, "name": name, "role": role, "backend": backend, "parent_id": parent_id or None,
            "status": "idle", "workdir": str(work), "model": model, "instructions": text,
            "session_id": session_id, "created_by": self.me["id"] if self.me else f"lead:{self.host}",
            "meta": json.dumps(meta)})
        out: dict[str, Any] = {"agent_id": aid, "name": name, "role": role, "backend": backend,
                               "workdir": str(work), "inbox": str(inbox), "outbox": str(outbox)}
        if task:
            out["turn"] = await self.send(aid, task)
        return out

    async def adopt(self, app: str, conversation_id: str, name: str, role: str = "manager") -> dict:
        self._require_lead()
        if app not in APP_BACKEND:
            raise FleetError(f"app must be one of {', '.join(APP_BACKEND)}")
        info = native.conversation_info(app, conversation_id)
        workdir = info["cwd"] if info["cwd"] and Path(info["cwd"]).is_dir() else ""
        return await self.spawn(name, APP_BACKEND[app], role=role, workdir=workdir, session_id=conversation_id)

    def _queue(self, aid: str) -> asyncio.Queue:
        if aid not in self.queues:
            self.queues[aid] = asyncio.Queue()
            self.workers[aid] = asyncio.create_task(self._worker(aid))
        return self.queues[aid]

    async def send(self, agent_id: str, text: str, wait_seconds: float = 0) -> dict:
        a = self._get(agent_id)
        self._check_control(a)
        a = self._own(a)
        if a["status"] == "stopped":
            raise FleetError("That agent is stopped.")
        tid = "tn-" + uuid.uuid4().hex[:10]
        self.store.add_turn(tid, a["id"], text)
        self._queue(a["id"]).put_nowait(tid)
        if wait_seconds > 0:
            await self.wait([a["id"]], wait_seconds)
        return self._turn_view(self.store.get_turn(tid))

    async def _worker(self, aid: str) -> None:
        q = self.queues[aid]
        while True:
            tid = await q.get()
            a = self.store.get_agent(aid)
            turn = self.store.get_turn(tid)
            if not a or not turn or a["status"] == "stopped" or turn["status"] != "queued":
                if turn and turn["status"] == "queued":
                    self.store.update_turn(tid, status="failed", error="agent stopped", finished_at=time.time())
                continue
            self.store.update_turn(tid, status="running", started_at=time.time())
            self.store.update_agent(aid, status="running")
            self.store.add_event(aid, tid, "user", turn["input"])
            req_agent = dict(a)
            req_agent["_history"] = self.store.history_messages(aid)

            def emit(kind: str, text: str, data: dict | None = None, _aid=aid, _tid=tid) -> None:
                self.store.add_event(_aid, _tid, kind, text, data)

            backend = self.backends.get(a["backend"])
            try:
                if backend is None:
                    raise BackendError(f"backend '{a['backend']}' is disabled or unknown")
                out = await backend.run_turn(TurnRequest(agent=req_agent, turn_id=tid, text=turn["input"],
                                                         emit=emit, timeout=self.turn_timeout,
                                                         first_turn=not a.get("session_id")))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                out = TurnOutcome("failed", "", f"{type(e).__name__}: {e}")
            self.store.update_turn(tid, status=out.status, output=out.output, error=out.error,
                                   finished_at=time.time())
            if out.error:
                emit("error", out.error)
            cur = self.store.get_agent(aid) or a
            fields: dict[str, Any] = {"status": "stopped" if cur["status"] == "stopped" else "idle",
                                      "last_error": out.error}
            if out.session_id and out.session_id != a.get("session_id"):
                fields["session_id"] = out.session_id
            self.store.update_agent(aid, **fields)

    def _busy(self, aid: str) -> bool:
        a = self.store.get_agent(aid)
        if not a:
            return False
        t = self.store.latest_turn(aid)
        if not t or t["status"] not in ("queued", "running"):
            return False
        meta = agent_meta(a)
        return meta.get("owner") == self.key or self._alive(meta.get("owner"))

    async def wait(self, agent_ids: list[str], timeout_seconds: float = 50, mode: str = "all") -> dict:
        for aid in agent_ids:
            self._check_read(self._get(aid))
        deadline = time.time() + max(0.0, min(float(timeout_seconds), self.max_wait))
        while True:
            busy = [aid for aid in agent_ids if self._busy(aid)]
            done = not busy if mode == "all" else len(busy) < len(agent_ids)
            if done or time.time() >= deadline:
                return {"done": done, "still_working": busy,
                        "agents": [self._agent_view(self._get(aid)) for aid in agent_ids]}
            await asyncio.sleep(1)

    async def get(self, agent_id: str) -> dict:
        a = self._get(agent_id)
        self._check_read(a)
        view = self._agent_view(a, 40000)
        view["children"] = [c["id"] for c in self.store.children_of(a["id"], live_only=False)]
        return view

    async def transcript(self, agent_id: str, after_event_id: int = 0, limit: int = 100) -> dict:
        self._check_read(self._get(agent_id))
        evs = self.store.events(agent_id, after_event_id, max(1, min(limit, 500)))
        items = [{"id": e["id"], "ts": e["ts"], "turn_id": e["turn_id"], "kind": e["kind"],
                  "text": _short(e["text"], 8000)} for e in evs]
        return {"events": items, "next_after_event_id": items[-1]["id"] if items else after_event_id}

    async def interrupt(self, agent_id: str) -> dict:
        a = self._get(agent_id)
        self._check_control(a)
        b = self.backends.get(a["backend"])
        return {"interrupted": bool(b and agent_meta(a).get("owner") == self.key and await b.interrupt(a))}

    async def stop(self, agent_id: str) -> dict:
        a = self._get(agent_id)
        self._check_control(a)
        stopped = []
        for c in self.store.children_of(a["id"]):
            stopped += (await self._stop_one(c))
        return {"stopped": stopped + await self._stop_one(a)}

    async def _stop_one(self, a: dict) -> list[str]:
        self.store.update_agent(a["id"], status="stopped")
        b = self.backends.get(a["backend"])
        if b and agent_meta(a).get("owner") == self.key:
            await b.interrupt(a)
            await b.stop(a)
        return [a["id"]]

    # -- files ------------------------------------------------------------------------------
    def _src_roots(self) -> list[Path]:
        if self.role == "lead":
            return expand_roots(self.cfg["files"]["allowed_roots"]) + [fleet_home().resolve()]
        meta = agent_meta(self.me)
        return expand_roots([self.me["workdir"], meta["inbox"], meta["outbox"], str(exchange_dir())])

    @staticmethod
    def _copy(src: Path, dest_dir: Path) -> str:
        dest_dir.mkdir(parents=True, exist_ok=True)
        target = dest_dir / src.name
        if src.is_dir():
            shutil.copytree(src, target, dirs_exist_ok=True)
        else:
            shutil.copy2(src, target)
        return str(target)

    async def send_files(self, agent_id: str, paths: list[str], subfolder: str = "") -> dict:
        a = self._get(agent_id)
        self._check_control(a)
        inbox = Path(agent_meta(a)["inbox"])
        dest = (inbox / subfolder).resolve() if subfolder else inbox
        if not str(dest).startswith(str(inbox.resolve())):
            raise FleetError("subfolder must stay inside the agent's inbox.")
        roots = self._src_roots()
        copied = [self._copy(check_path(p, roots, must_exist=True), dest) for p in paths]
        return {"inbox": str(dest), "copied": copied}

    async def fetch_files(self, agent_id: str, dest: str = "", paths: list[str] | None = None) -> dict:
        a = self._get(agent_id)
        self._check_control(a)
        meta = agent_meta(a)
        outbox = Path(meta["outbox"])
        if self.role == "lead":
            dest_dir = Path(os.path.expandvars(dest)).expanduser() if dest else exchange_dir() / f"{a['name']}-{a['id']}"
            if dest:
                dest_dir = check_path(str(dest_dir), self._src_roots(), must_exist=False)
        else:
            dest_dir = Path(agent_meta(self.me)["inbox"]) / f"from-{a['name']}-{a['id']}"
        agent_roots = expand_roots([a["workdir"], meta["outbox"], meta["inbox"]])
        if paths:
            srcs = [check_path(str(outbox / p) if not Path(p).is_absolute() else p, agent_roots, must_exist=True)
                    for p in paths]
        else:
            srcs = sorted(outbox.iterdir()) if outbox.exists() else []
        return {"dest": str(dest_dir), "copied": [self._copy(s, dest_dir) for s in srcs]}

    async def list_files(self, agent_id: str, where: str = "outbox") -> dict:
        a = self._get(agent_id)
        self._check_read(a)
        meta = agent_meta(a)
        root = Path({"outbox": meta["outbox"], "inbox": meta["inbox"], "workdir": a["workdir"]}.get(where, meta["outbox"]))
        files = []
        for p in sorted(root.rglob("*")) if root.exists() else []:
            if p.is_file():
                files.append({"path": str(p.relative_to(root)), "bytes": p.stat().st_size})
            if len(files) >= 300:
                break
        return {"folder": str(root), "files": files}

    # -- native app history -----------------------------------------------------------------
    async def list_conversations(self, app: str, limit: int = 20, search: str = "") -> dict:
        self._require_lead()
        try:
            return {"app": app, "conversations": native.list_conversations(app, max(1, min(limit, 100)), search)}
        except ValueError as e:
            raise FleetError(str(e))

    async def read_conversation(self, app: str, conversation_id: str, max_chars: int = 40000) -> dict:
        self._require_lead()
        try:
            return native.read_conversation(app, conversation_id, max(1000, min(max_chars, 200000)))
        except ValueError as e:
            raise FleetError(str(e))

    async def shutdown(self) -> None:
        for aid, task in list(self.workers.items()):
            task.cancel()
        with self.store._lock:
            self.store.db.execute("update turns set status='failed', error='its app closed before it finished', "
                                  "finished_at=? where status in ('queued','running') and agent_id in "
                                  f"({','.join('?' * len(self.workers)) or 'null'})",
                                  (time.time(), *self.workers))
        for aid in self.workers:
            a = self.store.get_agent(aid)
            if a and a["status"] == "running":
                self.store.update_agent(aid, status="idle")
        for b in self.backends.values():
            try:
                await b.shutdown()
            except Exception:
                pass

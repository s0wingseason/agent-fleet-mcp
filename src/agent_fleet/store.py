"""SQLite storage for agents, turns and transcript events."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

SCHEMA = """
create table if not exists agents(
  id text primary key, name text, role text, backend text, parent_id text, status text,
  workdir text, model text, instructions text, session_id text, created_by text,
  created_at real, updated_at real, last_error text, meta text default '{}');
create table if not exists turns(
  id text primary key, agent_id text, seq integer, status text, input text, output text,
  error text, created_at real, started_at real, finished_at real);
create table if not exists events(
  id integer primary key autoincrement, agent_id text, turn_id text, ts real,
  kind text, text text, data text);
create index if not exists ev_agent on events(agent_id, id);
create index if not exists turns_agent on turns(agent_id, seq);
"""

LIVE_STATUSES = ("idle", "running", "starting")


class Store:
    def __init__(self, path: Path):
        self._lock = threading.RLock()
        self.db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("pragma journal_mode=wal")
        self.db.execute("pragma busy_timeout=5000")
        self.db.executescript(SCHEMA)

    # -- agents ---------------------------------------------------------------------------
    def add_agent(self, rec: dict[str, Any]) -> dict[str, Any]:
        now = time.time()
        rec = {**rec, "created_at": now, "updated_at": now}
        rec.setdefault("meta", "{}")
        cols = ",".join(rec)
        with self._lock:
            self.db.execute(f"insert into agents({cols}) values({','.join('?' * len(rec))})",
                            list(rec.values()))
        return self.get_agent(rec["id"])

    def update_agent(self, agent_id: str, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = time.time()
        sets = ",".join(f"{k}=?" for k in fields)
        with self._lock:
            self.db.execute(f"update agents set {sets} where id=?", [*fields.values(), agent_id])

    def get_agent(self, agent_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.db.execute("select * from agents where id=?", (agent_id,)).fetchone()
        return dict(row) if row else None

    def list_agents(self, include_stopped: bool = False) -> list[dict[str, Any]]:
        q = "select * from agents"
        if not include_stopped:
            q += f" where status in ({','.join('?' * len(LIVE_STATUSES))})"
        q += " order by created_at"
        with self._lock:
            rows = self.db.execute(q, LIVE_STATUSES if not include_stopped else ()).fetchall()
        return [dict(r) for r in rows]

    def children_of(self, parent_id: str, live_only: bool = True) -> list[dict[str, Any]]:
        return [a for a in self.list_agents(include_stopped=not live_only) if a["parent_id"] == parent_id]

    def count_live(self) -> int:
        return len(self.list_agents())

    # -- turns ----------------------------------------------------------------------------
    def add_turn(self, turn_id: str, agent_id: str, text: str) -> dict[str, Any]:
        with self._lock:
            seq = self.db.execute("select coalesce(max(seq),0)+1 from turns where agent_id=?",
                                  (agent_id,)).fetchone()[0]
            self.db.execute(
                "insert into turns(id,agent_id,seq,status,input,created_at) values(?,?,?,?,?,?)",
                (turn_id, agent_id, seq, "queued", text, time.time()))
        return self.get_turn(turn_id)

    def update_turn(self, turn_id: str, **fields: Any) -> None:
        sets = ",".join(f"{k}=?" for k in fields)
        with self._lock:
            self.db.execute(f"update turns set {sets} where id=?", [*fields.values(), turn_id])

    def get_turn(self, turn_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.db.execute("select * from turns where id=?", (turn_id,)).fetchone()
        return dict(row) if row else None

    def latest_turn(self, agent_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.db.execute("select * from turns where agent_id=? order by seq desc limit 1",
                                  (agent_id,)).fetchone()
        return dict(row) if row else None

    def fail_unfinished_turns(self, reason: str) -> None:
        with self._lock:
            self.db.execute("update turns set status='failed', error=?, finished_at=? "
                            "where status in ('queued','running')", (reason, time.time()))

    # -- events ---------------------------------------------------------------------------
    def add_event(self, agent_id: str, turn_id: str | None, kind: str, text: str,
                  data: dict | None = None) -> int:
        with self._lock:
            cur = self.db.execute(
                "insert into events(agent_id,turn_id,ts,kind,text,data) values(?,?,?,?,?,?)",
                (agent_id, turn_id, time.time(), kind, text, json.dumps(data) if data else None))
            return int(cur.lastrowid)

    def events(self, agent_id: str, after_id: int = 0, limit: int = 200,
               kinds: list[str] | None = None) -> list[dict[str, Any]]:
        q = "select * from events where agent_id=? and id>?"
        args: list[Any] = [agent_id, after_id]
        if kinds:
            q += f" and kind in ({','.join('?' * len(kinds))})"
            args += kinds
        q += " order by id limit ?"
        args.append(limit)
        with self._lock:
            rows = self.db.execute(q, args).fetchall()
        return [dict(r) for r in rows]

    def history_messages(self, agent_id: str) -> list[dict[str, str]]:
        """user/assistant pairs, used to rebuild chat history for stateless backends."""
        with self._lock:
            rows = self.db.execute(
                "select kind, text from events where agent_id=? and kind in ('user','assistant') "
                "order by id", (agent_id,)).fetchall()
        return [{"role": r["kind"], "content": r["text"]} for r in rows]

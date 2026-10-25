"""Common interface every backend implements."""

from __future__ import annotations

import json
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from agent_fleet.paths import SERVER_NAME, fleet_home, self_command

Emit = Callable[[str, str, "dict | None"], None]  # (kind, text, data)


@dataclass
class TurnRequest:
    agent: dict[str, Any]
    turn_id: str
    text: str
    emit: Emit
    timeout: float
    first_turn: bool = False


@dataclass
class TurnOutcome:
    status: str  # completed | failed | interrupted
    output: str = ""
    error: str | None = None
    session_id: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class BackendError(Exception):
    pass


def agent_meta(agent: dict[str, Any]) -> dict[str, Any]:
    try:
        return json.loads(agent.get("meta") or "{}")
    except json.JSONDecodeError:
        return {}


def agent_paths(agent: dict[str, Any]) -> tuple[Path, Path, Path]:
    """(workdir, inbox, outbox) for an agent."""
    meta = agent_meta(agent)
    return Path(agent["workdir"]), Path(meta["inbox"]), Path(meta["outbox"])


def extra_dirs(agent: dict[str, Any]) -> list[str]:
    """Folders outside the workdir the agent must be able to write (its inbox/outbox)."""
    work, inbox, outbox = agent_paths(agent)
    out = []
    for p in (inbox.parent,):
        try:
            p.relative_to(work)
        except ValueError:
            out.append(str(p))
    return out


def agent_env(agent: dict[str, Any]) -> dict[str, str]:
    env = dict(os.environ)
    env["FLEET_AGENT_ID"] = agent["id"]
    env["FLEET_ROLE"] = agent["role"]
    env["AGENT_FLEET_HOME"] = str(fleet_home())
    return env


def shim_args(host: str, agent_id: str) -> list[str]:
    """argv that starts this program's stdio MCP shim, tagged with the calling agent."""
    return [*self_command(), "serve", "--host", host, "--agent-id", agent_id]


class Backend(ABC):
    type_name = "base"
    label = "base"

    def __init__(self, name: str, cfg: dict[str, Any]):
        self.name = name
        self.cfg = cfg

    @abstractmethod
    async def probe(self) -> dict[str, Any]:
        """{"available": bool, "version": str, "detail": str}"""

    async def start(self, agent: dict[str, Any]) -> dict[str, Any]:
        """Prepare a new agent. Return fields to store on the agent (e.g. session_id)."""
        return {}

    @abstractmethod
    async def run_turn(self, req: TurnRequest) -> TurnOutcome:
        ...

    async def interrupt(self, agent: dict[str, Any]) -> bool:
        return False

    async def stop(self, agent: dict[str, Any]) -> None:
        return None

    async def shutdown(self) -> None:
        return None

    @property
    def server_name(self) -> str:
        return SERVER_NAME

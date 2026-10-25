"""Where agent-fleet keeps its state, and how to re-launch itself.

The fleet home lives in a dot-folder under the user's home directory, not in AppData.
Microsoft Store (MSIX) builds of Claude Desktop and ChatGPT redirect AppData writes made by
their child processes into a private per-app folder. A shim launched by Claude would then see a
different token and broker file than a shim launched by ChatGPT. Home dot-folders (~/.codex,
~/.claude, ~/.gemini) are not redirected, which is why those apps use them too.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "agent-fleet"
SERVER_NAME = "agent-fleet"  # the key used in every host's MCP config


def fleet_home() -> Path:
    override = os.environ.get("AGENT_FLEET_HOME")
    base = Path(override).expanduser() if override else Path.home() / ".agent-fleet"
    base.mkdir(parents=True, exist_ok=True)
    return base


def config_path() -> Path:
    return fleet_home() / "config.toml"


def token_path() -> Path:
    return fleet_home() / "token"


def broker_info_path() -> Path:
    return fleet_home() / "broker.json"


def db_path() -> Path:
    return fleet_home() / "fleet.db"


def logs_dir() -> Path:
    p = fleet_home() / "logs"
    p.mkdir(parents=True, exist_ok=True)
    return p


def agents_dir() -> Path:
    p = fleet_home() / "agents"
    p.mkdir(parents=True, exist_ok=True)
    return p


def exchange_dir() -> Path:
    """A shared drop folder every agent and the lead may read and write."""
    p = fleet_home() / "exchange"
    p.mkdir(parents=True, exist_ok=True)
    return p


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def self_command() -> list[str]:
    """argv prefix that re-launches this program (installed exe or `python -m agent_fleet`)."""
    if is_frozen():
        return [sys.executable]
    return [sys.executable, "-m", "agent_fleet"]

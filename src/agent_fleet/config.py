"""Configuration: ~/.agent-fleet/config.toml merged over built-in defaults."""

from __future__ import annotations

import copy
import tomllib
from pathlib import Path
from typing import Any

from agent_fleet.paths import config_path

DEFAULTS: dict[str, Any] = {
    "broker": {"host": "127.0.0.1", "port": 47821},
    "limits": {
        "max_live_agents": 12,
        "max_children_per_manager": 6,
        "turn_timeout_seconds": 1800,
        "max_wait_seconds": 55,
    },
    "files": {
        # Folders the lead may send files from or fetch files into, besides the fleet's own
        # folders. Managers and children are limited to fleet folders.
        "allowed_roots": ["~/Desktop", "~/Documents", "~/Downloads"],
        "max_copy_mb": 500,
    },
    "backends": {
        "codex": {
            "type": "codex",
            "enabled": True,
            "binary": "",
            "model": "",
            "sandbox": "workspace-write",
            "approval": "never",
            "visible_in_app": True,
        },
        "antigravity": {
            "type": "antigravity",
            "enabled": True,
            "binary": "",
            "model": "",
            "mode": "accept-edits",
            "sandbox": True,
            "skip_permissions": False,
        },
        "claude-code": {
            "type": "claude-code",
            "enabled": True,
            "binary": "",
            "model": "",
            "permission_mode": "acceptEdits",
        },
        "ollama": {
            "type": "openai",
            "enabled": True,
            "base_url": "http://127.0.0.1:11434/v1",
            "api_key": "",
            "model": "",
            "workspace_tools": True,
        },
        "lmstudio": {
            "type": "openai",
            "enabled": True,
            "base_url": "http://127.0.0.1:1234/v1",
            "api_key": "",
            "model": "",
            "workspace_tools": True,
        },
        "vllm": {
            "type": "openai",
            "enabled": False,
            "base_url": "http://127.0.0.1:8000/v1",
            "api_key": "",
            "model": "",
            "workspace_tools": True,
        },
    },
    "desktop": {
        # Opt-in. Lets the lead look at and type into ChatGPT / Antigravity windows you already
        # have open. Read the README's note on OpenAI's Terms of Use before turning this on.
        "enabled": False,
        "allow_typing": False,
    },
}

DEFAULT_FILE = """# agent-fleet configuration. Delete a line to fall back to its default.
# Full reference: https://github.com/s0wingseason/agent-fleet-mcp/blob/main/docs/CONFIG.md

[broker]
host = "127.0.0.1"   # keep this on loopback unless you know why you're changing it
port = 47821

[limits]
max_live_agents = 12
max_children_per_manager = 6
turn_timeout_seconds = 1800

[files]
allowed_roots = ["~/Desktop", "~/Documents", "~/Downloads"]

[backends.codex]
enabled = true
model = ""                 # empty = your Codex / ChatGPT default
sandbox = "workspace-write" # read-only | workspace-write | danger-full-access

[backends.antigravity]
enabled = true
model = ""
mode = "accept-edits"
sandbox = true

[backends.claude-code]
enabled = true
model = ""
permission_mode = "acceptEdits"

[backends.ollama]
enabled = true
base_url = "http://127.0.0.1:11434/v1"
model = ""

[backends.lmstudio]
enabled = true
base_url = "http://127.0.0.1:1234/v1"
model = ""

[backends.vllm]
enabled = false
base_url = "http://127.0.0.1:8000/v1"
model = ""

[desktop]
enabled = false
allow_typing = false
"""


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: Path | None = None) -> dict[str, Any]:
    path = path or config_path()
    if not path.exists():
        path.write_text(DEFAULT_FILE, encoding="utf-8")
    try:
        user = tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError):
        user = {}
    cfg = _merge(DEFAULTS, user)
    # A user-defined backend without a type is assumed to be OpenAI-compatible.
    for name, b in cfg["backends"].items():
        b.setdefault("type", "openai")
        b.setdefault("enabled", True)
    return cfg

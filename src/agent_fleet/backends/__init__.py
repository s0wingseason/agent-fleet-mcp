from __future__ import annotations

from typing import Any

from agent_fleet.backends.base import Backend


def make_backend(name: str, cfg: dict[str, Any]) -> Backend:
    from agent_fleet.backends.antigravity import AntigravityBackend
    from agent_fleet.backends.claude_code import ClaudeCodeBackend
    from agent_fleet.backends.codex import CodexBackend
    from agent_fleet.backends.openai_compat import OpenAICompatBackend

    kinds = {"codex": CodexBackend, "antigravity": AntigravityBackend,
             "claude-code": ClaudeCodeBackend, "openai": OpenAICompatBackend}
    return kinds.get(cfg.get("type", "openai"), OpenAICompatBackend)(name, cfg)

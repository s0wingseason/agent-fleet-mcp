"""Add (or remove) agent-fleet in every MCP-capable app found on this computer."""

from __future__ import annotations

import glob
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from agent_fleet.paths import SERVER_NAME, self_command


def _entry(host: str) -> tuple[str, list[str]]:
    cmd = self_command()
    return cmd[0], cmd[1:] + ["serve", "--host", host]


def _targets() -> dict[str, list[tuple[Path, str, str]]]:
    h = Path.home()
    env = lambda n: Path(os.environ.get(n, str(h)))  # noqa: E731
    win, mac = os.name == "nt", sys.platform == "darwin"
    claude = []
    if win:
        claude += [Path(p) / "LocalCache" / "Roaming" / "Claude" / "claude_desktop_config.json"
                   for p in glob.glob(str(env("LOCALAPPDATA") / "Packages" / "Claude_*"))]
        claude.append(env("APPDATA") / "Claude" / "claude_desktop_config.json")
    elif mac:
        claude.append(h / "Library/Application Support/Claude/claude_desktop_config.json")
    else:
        claude.append(h / ".config/Claude/claude_desktop_config.json")
    code = env("APPDATA") / "Code/User" if win else (h / "Library/Application Support/Code/User" if mac else h / ".config/Code/User")
    return {
        "claude-desktop": [(p, "json", "mcpServers") for p in claude],
        "codex": [(Path(os.environ.get("CODEX_HOME") or h / ".codex") / "config.toml", "toml", "mcp_servers")],
        "antigravity": [(h / ".gemini/antigravity/mcp_config.json", "json", "mcpServers"),
                        (h / ".gemini/config/mcp_config.json", "json", "mcpServers")],
        "gemini-cli": [(h / ".gemini/settings.json", "json", "mcpServers")],
        "vscode": [(code / "mcp.json", "json", "servers")],
        "cursor": [(h / ".cursor/mcp.json", "json", "mcpServers")],
        "windsurf": [(h / ".codeium/windsurf/mcp_config.json", "json", "mcpServers")],
        "lm-studio": [(h / ".lmstudio/mcp.json", "json", "mcpServers")],
        "hermes": [(h / ".hermes/config.yaml", "yaml", "mcp_servers")],
    }


LABELS = {"claude-desktop": "Claude Desktop", "claude-code": "Claude Code", "codex": "ChatGPT desktop / Codex",
          "antigravity": "Google Antigravity", "gemini-cli": "Gemini CLI", "vscode": "VS Code",
          "cursor": "Cursor", "windsurf": "Windsurf", "lm-studio": "LM Studio", "hermes": "Hermes Agent"}


def _present(path: Path, host: str) -> bool:
    if host == "gemini-cli":
        return path.exists()
    return path.exists() or path.parent.is_dir()


def detect() -> dict[str, list[Path]]:
    found = {host: [p for p, _, _ in ts if _present(p, host)] for host, ts in _targets().items()}
    found = {k: v for k, v in found.items() if v}
    if shutil.which("claude") or (Path.home() / ".claude.json").exists():
        found["claude-code"] = [Path.home() / ".claude.json"]
    return found


def _backup(p: Path) -> None:
    b = p.with_name(p.name + ".agent-fleet.bak")
    if p.exists() and not b.exists():
        shutil.copy2(p, b)


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".agent-fleet.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, p)


def _edit(p: Path, kind: str, key: str, host: str, remove: bool) -> str:
    cmd, args = _entry(host)
    if kind == "json":
        data: dict[str, Any] = json.loads(p.read_text(encoding="utf-8-sig") or "{}") if p.exists() else {}
        servers = data.setdefault(key, {})
        entry: dict[str, Any] = {"command": cmd, "args": args}
        if host == "vscode":
            entry = {"type": "stdio", **entry}
        if remove:
            if SERVER_NAME not in servers:
                return "not present"
            del servers[SERVER_NAME]
        elif servers.get(SERVER_NAME) == entry:
            return "unchanged"
        else:
            servers[SERVER_NAME] = entry
        _backup(p)
        _write(p, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    elif kind == "toml":
        import tomlkit
        doc = tomlkit.parse(p.read_text(encoding="utf-8")) if p.exists() else tomlkit.document()
        if key not in doc:
            doc[key] = tomlkit.table(is_super_table=True)
        servers = doc[key]
        if remove:
            if SERVER_NAME not in servers:
                return "not present"
            del servers[SERVER_NAME]
        else:
            t = tomlkit.table()
            t.update({"command": cmd, "args": args, "startup_timeout_sec": 30, "tool_timeout_sec": 120})
            if SERVER_NAME in servers and servers[SERVER_NAME].unwrap() == t.unwrap():
                return "unchanged"
            servers[SERVER_NAME] = t
        _backup(p)
        _write(p, tomlkit.dumps(doc))
    else:
        from ruamel.yaml import YAML
        y = YAML()
        data = (y.load(p.read_text(encoding="utf-8")) if p.exists() else None) or {}
        servers = data.setdefault(key, {}) or {}
        data[key] = servers
        if remove:
            if SERVER_NAME not in servers:
                return "not present"
            del servers[SERVER_NAME]
        else:
            servers[SERVER_NAME] = {"command": cmd, "args": args}
        _backup(p)
        import io
        buf = io.StringIO()
        y.dump(data, buf)
        _write(p, buf.getvalue())
    return "removed" if remove else "installed"


def _claude_code(remove: bool) -> str:
    exe = shutil.which("claude")
    if exe:
        subprocess.run([exe, "mcp", "remove", "--scope", "user", SERVER_NAME], capture_output=True, timeout=60)
        if remove:
            return "removed"
        cmd, args = _entry("claude-code")
        r = subprocess.run([exe, "mcp", "add", "--scope", "user", SERVER_NAME, "--", cmd, *args],
                           capture_output=True, text=True, timeout=60)
        return "installed" if r.returncode == 0 else f"error: {(r.stderr or r.stdout).strip()[:300]}"
    p = Path.home() / ".claude.json"
    return _edit(p, "json", "mcpServers", "claude-code", remove) if p.exists() else "skipped (not found)"


def run(hosts: list[str] | None, remove: bool = False, dry_run: bool = False) -> list[dict[str, str]]:
    found = detect()
    targets = _targets()
    chosen = hosts or list(found)
    results = []
    for host in chosen:
        label = LABELS.get(host, host)
        if host not in found:
            results.append({"app": label, "file": "", "result": "skipped (not found on this computer)"})
            continue
        if host == "claude-code":
            results.append({"app": label, "file": "claude mcp (user scope)",
                            "result": "would update" if dry_run else _claude_code(remove)})
            continue
        for p, kind, key in targets[host]:
            if p not in found[host]:
                continue
            try:
                res = "would update" if dry_run else _edit(p, kind, key, host, remove)
            except Exception as e:
                res = f"error: {type(e).__name__}: {e}"
            results.append({"app": label, "file": str(p), "result": res})
    return results

"""Read conversation history the apps keep on disk: Codex/ChatGPT, Claude Code, Antigravity."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Iterator

APPS = ("codex", "claude-code", "antigravity")
SKIP = ("<environment_context>", "<user_instructions>", "# AGENTS.md", "<permissions", "<collaboration_mode")


def _home() -> Path:
    return Path.home()


def _lines(p: Path, limit: int | None = None) -> Iterator[dict]:
    with open(p, encoding="utf-8", errors="replace") as f:
        for i, line in enumerate(f):
            if limit and i >= limit:
                break
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    return "\n".join(c["text"] for c in content or [] if isinstance(c, dict) and isinstance(c.get("text"), str))


def _codex_msgs(p: Path, limit: int | None = None) -> Iterator[tuple[str, str]]:
    for ev in _lines(p, limit):
        pl = ev.get("payload") or {}
        if ev.get("type") != "response_item":
            continue
        if pl.get("type") == "message" and pl.get("role") in ("user", "assistant"):
            t = _text(pl.get("content")).strip()
            if t and not t.startswith(SKIP):
                yield pl["role"], t
        elif pl.get("type") in ("function_call", "custom_tool_call", "local_shell_call"):
            arg = pl.get("arguments") or pl.get("input") or pl.get("action") or ""
            yield "tool", f"{pl.get('name') or pl.get('type')}: {str(arg)[:300]}"


def _claude_msgs(p: Path, limit: int | None = None) -> Iterator[tuple[str, str]]:
    for ev in _lines(p, limit):
        if ev.get("type") not in ("user", "assistant") or ev.get("isMeta"):
            continue
        m = ev.get("message") or {}
        c = m.get("content")
        role = m.get("role", ev["type"])
        if isinstance(c, str) and c.strip():
            yield role, c
        elif isinstance(c, list):
            for b in c:
                if b.get("type") == "text" and b.get("text", "").strip():
                    yield role, b["text"]
                elif b.get("type") == "tool_use":
                    yield "tool", f"{b.get('name')}: {json.dumps(b.get('input'))[:300]}"


def _varint(b: bytes, i: int) -> tuple[int, int]:
    r = s = 0
    while i < len(b):
        x = b[i]
        i += 1
        r |= (x & 0x7F) << s
        s += 7
        if not x & 0x80:
            return r, i
    raise ValueError


def _printable(s: str) -> bool:
    return len(s) >= 2 and sum(ch.isprintable() or ch in "\n\t\r" for ch in s) >= 0.95 * len(s)


def _pb_strings(b: bytes, depth: int = 0) -> list[str] | None:
    """Best-effort: pull readable strings out of an unknown protobuf blob."""
    out: list[str] = []
    i = 0
    try:
        while i < len(b):
            key, i = _varint(b, i)
            wt = key & 7
            if wt == 0:
                _, i = _varint(b, i)
            elif wt == 1:
                i += 8
            elif wt == 5:
                i += 4
            elif wt == 2:
                n, i = _varint(b, i)
                chunk = b[i:i + n]
                i += n
                if len(chunk) != n:
                    raise ValueError
                try:
                    s = chunk.decode("utf-8")
                except UnicodeDecodeError:
                    s = ""
                if s and _printable(s) and (" " in s or len(s) > 24):
                    out.append(s)
                    continue
                sub = _pb_strings(chunk, depth + 1) if depth < 12 else None
                if sub:
                    out += sub
                elif s and _printable(s):
                    out.append(s)
            else:
                raise ValueError
            if i > len(b):
                raise ValueError
    except (ValueError, IndexError):
        return None if depth else out
    return out


def _files(app: str) -> list[Path]:
    h = _home()
    if app == "codex":
        root = Path(os.environ.get("CODEX_HOME") or h / ".codex") / "sessions"
        files = list(root.rglob("rollout-*.jsonl")) if root.exists() else []
    elif app == "claude-code":
        root = h / ".claude" / "projects"
        files = list(root.glob("*/*.jsonl")) if root.exists() else []
    elif app == "antigravity":
        root = h / ".gemini" / "antigravity" / "conversations"
        files = list(root.glob("*.db")) if root.exists() else []
    else:
        raise ValueError(f"app must be one of {', '.join(APPS)}")
    return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)


def _conv_id(app: str, p: Path) -> str:
    return p.stem[-36:] if app == "codex" else p.stem


def _ag_steps(p: Path) -> list[tuple[int, int, list[str]]]:
    con = sqlite3.connect(f"file:{p.as_posix()}?mode=ro&immutable=1", uri=True)
    try:
        rows = con.execute("select idx, step_type, step_payload from steps order by idx").fetchall()
    finally:
        con.close()
    out = []
    for idx, st, payload in rows:
        strings = _pb_strings(payload or b"") or []
        seen, uniq = set(), []
        for s in strings:
            if s not in seen:
                seen.add(s)
                uniq.append(s)
        out.append((idx, st, uniq))
    return out


def _messages(app: str, p: Path, limit: int | None = None) -> Iterator[tuple[str, str]]:
    if app == "codex":
        yield from _codex_msgs(p, limit)
    elif app == "claude-code":
        yield from _claude_msgs(p, limit)
    else:
        for idx, st, strings in _ag_steps(p):
            if strings:
                yield f"step {idx} (type {st})", "\n".join(strings)


def conversation_info(app: str, conv_id: str) -> dict[str, Any]:
    for p in _files(app):
        if _conv_id(app, p) == conv_id:
            cwd = None
            if app == "codex":
                for ev in _lines(p, 5):
                    if ev.get("type") == "session_meta":
                        cwd = (ev.get("payload") or {}).get("cwd")
            elif app == "claude-code":
                for ev in _lines(p, 20):
                    cwd = cwd or ev.get("cwd")
            return {"path": p, "cwd": cwd}
    raise ValueError(f"No {app} conversation with id {conv_id}")


def list_conversations(app: str, limit: int = 20, search: str = "") -> list[dict[str, Any]]:
    out = []
    for p in _files(app):
        if len(out) >= limit:
            break
        try:
            first = next((t for r, t in _messages(app, p, 400) if r == "user" or app == "antigravity"), "")
        except Exception:
            first = ""
        if search and search.lower() not in first.lower():
            continue
        out.append({"id": _conv_id(app, p), "updated": p.stat().st_mtime, "preview": first[:200]})
    return out


def read_conversation(app: str, conv_id: str, max_chars: int = 40000) -> dict[str, Any]:
    info = conversation_info(app, conv_id)
    msgs, used, truncated = [], 0, False
    for role, text in _messages(app, info["path"]):
        if used + len(text) > max_chars:
            truncated = True
            break
        msgs.append({"role": role, "text": text})
        used += len(text)
    note = "Antigravity history is stored in an undocumented format; this text is extracted best-effort." if app == "antigravity" else None
    return {"app": app, "id": conv_id, "cwd": info["cwd"], "messages": msgs, "truncated": truncated, "note": note}

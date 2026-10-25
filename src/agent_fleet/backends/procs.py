"""Locate the real CLI binaries for each backend.

PATH is not trusted blindly. On some machines `codex` on PATH is an unrelated program (for
example the "codex" comic-archive server on PyPI), so a candidate only counts if its
`--version` output looks like the tool we want.
"""

from __future__ import annotations

import glob
import os
import shutil
import subprocess
import sys
from pathlib import Path

_cache: dict[str, tuple[str, str] | None] = {}

NO_WINDOW = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW


def _version(path: str) -> str | None:
    try:
        out = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=20,
                             stdin=subprocess.DEVNULL, creationflags=NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired):
        return None
    text = (out.stdout or out.stderr or "").strip().splitlines()
    return text[0].strip() if text else None


def _candidates_codex() -> list[str]:
    c: list[str] = []
    if os.name == "nt":
        appdata = os.environ.get("APPDATA", "")
        pattern = os.path.join(appdata, "npm", "node_modules", "@openai", "codex", "node_modules",
                               "@openai", "codex-win32-*", "vendor", "*", "bin", "codex.exe")
        c += sorted(glob.glob(pattern))
    w = shutil.which("codex")
    if w:
        c.append(w)
    for extra in ("~/.local/bin/codex", "/opt/homebrew/bin/codex", "/usr/local/bin/codex"):
        p = os.path.expanduser(extra)
        if os.path.exists(p):
            c.append(p)
    return c


def _candidates_agy() -> list[str]:
    c: list[str] = []
    w = shutil.which("agy")
    if w:
        c.append(w)
    if os.name == "nt":
        c.append(os.path.join(os.environ.get("LOCALAPPDATA", ""), "agy", "bin", "agy.exe"))
    c.append(os.path.expanduser("~/.local/bin/agy"))
    return [p for p in c if p and os.path.exists(p)]


def _candidates_claude() -> list[str]:
    c: list[str] = []
    w = shutil.which("claude")
    if w:
        c.append(w)
    c.append(os.path.expanduser("~/.local/bin/claude.exe" if os.name == "nt" else "~/.local/bin/claude"))
    return [p for p in c if p and os.path.exists(p)]


CHECKS = {
    "codex": (_candidates_codex, lambda v: v.lower().startswith("codex-cli")),
    "agy": (_candidates_agy, lambda v: bool(v) and v[0].isdigit()),
    "claude": (_candidates_claude, lambda v: "claude code" in v.lower()),
}


def find_binary(kind: str, configured: str = "") -> tuple[str, str] | None:
    """Return (path, version) for a working binary, or None."""
    key = f"{kind}|{configured}"
    if key in _cache:
        return _cache[key]
    finder, looks_right = CHECKS[kind]
    cands = ([os.path.expanduser(configured)] if configured else []) + finder()
    seen = set()
    result = None
    for cand in cands:
        norm = os.path.normcase(os.path.abspath(cand))
        if norm in seen or not os.path.exists(cand):
            continue
        seen.add(norm)
        v = _version(cand)
        if v and looks_right(v):
            result = (cand, v)
            break
    _cache[key] = result
    return result


def describe_imposters(kind: str) -> list[str]:
    """Binaries named like `kind` on PATH that are not the real tool (for `doctor`)."""
    finder, looks_right = CHECKS[kind]
    bad = []
    for cand in finder():
        v = _version(cand)
        if not v or not looks_right(v):
            bad.append(f"{cand} -> {v or 'no version output'}")
    return bad


def python_scripts_dir() -> Path:
    return Path(sys.executable).parent / "Scripts"

"""Token handling, request checks and file-path policy."""

from __future__ import annotations

import hmac
import os
import secrets
import stat
from pathlib import Path

from agent_fleet.paths import fleet_home, token_path

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1"}

# Never copied in or out, for anyone. Matched case-insensitively against each path part.
DENY_DIR_NAMES = {
    ".ssh", ".gnupg", ".aws", ".azure", ".kube", ".docker", ".password-store",
    "credentials", "protect",  # %APPDATA%\Microsoft\Credentials, \Protect
}
DENY_FILE_NAMES = {
    ".env", "auth.json", ".credentials.json", "credentials.json", "oauth_creds.json",
    "login data", "cookies", "token", "broker.json", ".netrc", ".git-credentials",
}
DENY_SUFFIXES = {".pem", ".key", ".pfx", ".p12", ".kdbx", ".ppk"}
DENY_PREFIXES = ("id_rsa", "id_ed25519", "id_ecdsa", "id_dsa")


def load_or_create_token() -> str:
    p = token_path()
    if p.exists():
        tok = p.read_text(encoding="utf-8").strip()
        if len(tok) >= 32:
            return tok
    tok = secrets.token_urlsafe(32)
    p.write_text(tok, encoding="utf-8")
    if os.name != "nt":
        p.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return tok


def token_matches(given: str | None, expected: str) -> bool:
    if not given:
        return False
    return hmac.compare_digest(given.encode(), expected.encode())


def host_is_loopback(host_header: str | None) -> bool:
    """Blocks DNS-rebinding: the Host header must name the loopback interface."""
    if not host_header:
        return False
    host = host_header.strip().lower()
    if host.startswith("["):
        host = host.split("]")[0] + "]"
    elif ":" in host:
        host = host.rsplit(":", 1)[0]
    return host in LOOPBACK_HOSTS


def origin_allowed(origin: str | None) -> bool:
    """Browsers always send Origin on cross-site requests; MCP hosts usually send none."""
    if not origin or origin == "null":
        return origin is None
    try:
        scheme, rest = origin.split("://", 1)
    except ValueError:
        return False
    return scheme in ("http", "https") and host_is_loopback(rest)


class PathDenied(Exception):
    pass


def _is_denied(p: Path) -> bool:
    for part in p.parts:
        low = part.lower()
        if low in DENY_DIR_NAMES:
            return True
    name = p.name.lower()
    if name in DENY_FILE_NAMES or p.suffix.lower() in DENY_SUFFIXES:
        return True
    return name.startswith(DENY_PREFIXES)


def _within(p: Path, root: Path) -> bool:
    try:
        p.relative_to(root)
        return True
    except ValueError:
        return False


def expand_roots(roots: list[str]) -> list[Path]:
    out = []
    for r in roots:
        try:
            out.append(Path(os.path.expandvars(r)).expanduser().resolve())
        except OSError:
            continue
    return out


def check_path(raw: str, roots: list[Path], *, must_exist: bool) -> Path:
    """Resolve `raw` (following symlinks) and require it to sit inside one of `roots`."""
    p = Path(os.path.expandvars(raw)).expanduser()
    try:
        p = p.resolve(strict=must_exist)
    except FileNotFoundError:
        raise PathDenied(f"Not found: {raw}")
    if not any(_within(p, r) for r in roots):
        shown = ", ".join(str(r) for r in roots)
        raise PathDenied(f"{p} is outside the folders this caller may use ({shown}).")
    if _is_denied(p):
        raise PathDenied(f"{p} looks like a credential or key file and is never copied.")
    home = fleet_home().resolve()
    if p in (home / "token", home / "broker.json", home / "config.toml"):
        raise PathDenied("agent-fleet's own token, config and broker files are off limits.")
    return p

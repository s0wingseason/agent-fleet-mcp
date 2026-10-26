"""Command line: `agent-fleet serve | install | uninstall | doctor`."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from agent_fleet import __version__


def _print_results(rows: list[dict[str, str]]) -> None:
    if not rows:
        print("No supported apps found.")
    for r in rows:
        print(f"  {r['app']:<24} {r['result']:<14} {r['file']}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="agent-fleet", description="Run manager and child AI agents from any MCP app.")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd")
    s = sub.add_parser("serve", help="run the MCP server over stdio (apps call this)")
    s.add_argument("--host", default="unknown")
    s.add_argument("--agent-id", default="")
    i = sub.add_parser("install", help="add agent-fleet to every supported app found")
    i.add_argument("--hosts", default="", help="comma list, e.g. claude-desktop,codex,antigravity")
    i.add_argument("--dry-run", action="store_true")
    u = sub.add_parser("uninstall", help="remove agent-fleet from app configs")
    u.add_argument("--hosts", default="")
    sub.add_parser("doctor", help="show which apps and agent backends were found")
    a = p.parse_args(argv)

    if a.cmd == "serve":
        from agent_fleet.paths import logs_dir
        logging.basicConfig(filename=str(logs_dir() / "server.log"), level=logging.INFO,
                            format="%(asctime)s %(process)d %(name)s %(levelname)s %(message)s")
        from agent_fleet.server import build_server
        build_server(a.host, a.agent_id).run("stdio")
        return 0
    if a.cmd in ("install", "uninstall"):
        from agent_fleet.install import run
        hosts = [h.strip() for h in a.hosts.split(",") if h.strip()] or None
        rows = run(hosts, remove=a.cmd == "uninstall", dry_run=getattr(a, "dry_run", False))
        _print_results(rows)
        if a.cmd == "install":
            print("\nRestart each app (fully quit Claude Desktop / ChatGPT from the tray) to load agent-fleet.")
        return 0
    if a.cmd == "doctor":
        from agent_fleet.backends.procs import describe_imposters
        from agent_fleet.fleet import Fleet
        from agent_fleet.install import LABELS, detect
        print(f"agent-fleet {__version__}\n\nApps that can load it:")
        for host, paths in detect().items():
            print(f"  {LABELS.get(host, host):<24} {paths[0]}")
        st = asyncio.run(Fleet(host="doctor").status())
        print("\nAgent backends:")
        for name, b in st["backends"].items():
            print(f"  {name:<14} {'OK ' if b.get('available') else '-- '} {b.get('version', '')} {b.get('detail', '')}")
        bad = describe_imposters("codex")
        if bad:
            print("\nNote: these `codex` programs are not OpenAI's Codex CLI and are ignored:\n  " + "\n  ".join(bad))
        print(f"\nFleet folder: {st['fleet_folder']}")
        return 0
    p.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())

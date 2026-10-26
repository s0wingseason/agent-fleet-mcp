"""The MCP server (stdio) every host app launches."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Any, Awaitable

from mcp.server.fastmcp import FastMCP

from agent_fleet.backends.base import BackendError
from agent_fleet.fleet import Fleet, FleetError
from agent_fleet.security import PathDenied

INSTRUCTIONS = (
    "agent-fleet runs other AI agents for you. Usual flow: fleet_status -> spawn_agent(role='manager', "
    "backend=..., task=...) -> wait_for_agents (repeat until done) -> get_agent / get_transcript -> "
    "fetch_files. Managers can create their own child agents. No call blocks for more than about 55 "
    "seconds, so keep polling wait_for_agents for long tasks.")


def build_server(host: str, agent_id: str = "") -> FastMCP:
    fleet = Fleet(host=host, agent_id=agent_id or os.environ.get("FLEET_AGENT_ID") or None)

    @asynccontextmanager
    async def lifespan(_server: FastMCP):
        try:
            yield {}
        finally:
            await fleet.shutdown()

    mcp = FastMCP("agent-fleet", instructions=INSTRUCTIONS, lifespan=lifespan)

    async def guard(coro: Awaitable[Any]) -> Any:
        try:
            return await coro
        except (FleetError, PathDenied, BackendError, FileNotFoundError) as e:
            return {"error": str(e)}

    @mcp.tool()
    async def fleet_status() -> dict:
        """Who you are (lead, manager or child), which agent backends work on this PC, and the agents you can see."""
        return await guard(fleet.status())

    @mcp.tool()
    async def spawn_agent(name: str, backend: str, role: str = "manager", task: str = "",
                          instructions: str = "", parent_id: str = "", model: str = "",
                          workdir: str = "") -> dict:
        """Create an agent. backend: codex (ChatGPT/Codex), antigravity, claude-code, ollama, lmstudio,
        vllm, or another name from fleet_status. role: 'manager' (can create its own child agents) or
        'child'. Managers can only create children. If task is given it is sent as the first message.
        workdir (lead only) points the agent at an existing folder, e.g. a code repo."""
        return await guard(fleet.spawn(name, backend, role, task, instructions, parent_id, model, workdir))

    @mcp.tool()
    async def send_message(agent_id: str, text: str, wait_seconds: int = 0) -> dict:
        """Send the agent its next instruction. Returns at once unless wait_seconds > 0 (max ~55)."""
        return await guard(fleet.send(agent_id, text, wait_seconds))

    @mcp.tool()
    async def wait_for_agents(agent_ids: list[str], timeout_seconds: int = 50, mode: str = "all") -> dict:
        """Wait (max ~55s) until the agents finish their current work. mode 'all' or 'any'.
        Returns each agent's status and latest output; call again if done is false."""
        return await guard(fleet.wait(agent_ids, timeout_seconds, mode))

    @mcp.tool()
    async def get_agent(agent_id: str) -> dict:
        """Status, folders, children and latest output of one agent."""
        return await guard(fleet.get(agent_id))

    @mcp.tool()
    async def get_transcript(agent_id: str, after_event_id: int = 0, limit: int = 100) -> dict:
        """The agent's conversation log: your messages, its replies, tool activity and errors.
        Page with after_event_id."""
        return await guard(fleet.transcript(agent_id, after_event_id, limit))

    @mcp.tool()
    async def interrupt_agent(agent_id: str) -> dict:
        """Cancel what the agent is doing right now; it stays available for new messages."""
        return await guard(fleet.interrupt(agent_id))

    @mcp.tool()
    async def stop_agent(agent_id: str) -> dict:
        """Stop an agent for good (and its children)."""
        return await guard(fleet.stop(agent_id))

    @mcp.tool()
    async def send_files(agent_id: str, paths: list[str], subfolder: str = "") -> dict:
        """Copy files or folders into the agent's inbox. The lead may send from Desktop, Documents,
        Downloads (configurable) and fleet folders; managers from their own folders."""
        return await guard(fleet.send_files(agent_id, paths, subfolder))

    @mcp.tool()
    async def fetch_files(agent_id: str, dest: str = "", paths: list[str] | None = None) -> dict:
        """Copy the agent's deliverables (its outbox, or specific paths) to dest. Default dest is the
        fleet exchange folder for the lead, or your own inbox for a manager."""
        return await guard(fleet.fetch_files(agent_id, dest, paths))

    @mcp.tool()
    async def list_files(agent_id: str, where: str = "outbox") -> dict:
        """List files in an agent's outbox, inbox or workdir."""
        return await guard(fleet.list_files(agent_id, where))

    @mcp.tool()
    async def list_app_conversations(app: str, limit: int = 20, search: str = "") -> dict:
        """Lead only. List conversations saved on this PC by app: codex (ChatGPT desktop Codex mode and
        Codex CLI), claude-code, or antigravity."""
        return await guard(fleet.list_conversations(app, limit, search))

    @mcp.tool()
    async def read_app_conversation(app: str, conversation_id: str, max_chars: int = 40000) -> dict:
        """Lead only. Read one saved conversation from codex, claude-code or antigravity."""
        return await guard(fleet.read_conversation(app, conversation_id, max_chars))

    @mcp.tool()
    async def adopt_conversation(app: str, conversation_id: str, name: str, role: str = "manager") -> dict:
        """Lead only. Turn an existing codex, claude-code or antigravity conversation into a fleet agent
        so you can keep directing it with send_message."""
        return await guard(fleet.adopt(app, conversation_id, name, role))

    return mcp

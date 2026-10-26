# agent-fleet-mcp (beta)

An MCP server that lets a **lead** agent (you in Claude Desktop, ChatGPT, Antigravity, Claude Code, VS Code, ...) run **manager** agents, and lets managers run their own **child** agents — across:

| Backend | How it is driven | Needs |
|---|---|---|
| `codex` — ChatGPT desktop (Codex mode) / Codex CLI | OpenAI's official `codex app-server` JSON-RPC | `npm i -g @openai/codex`, signed in |
| `antigravity` — Google Antigravity | official `agy -p --output-format stream-json` | Antigravity CLI (`agy`), signed in |
| `claude-code` — Claude Code | `claude -p --output-format stream-json` | Claude Code CLI, signed in |
| `ollama`, `lmstudio`, `vllm`, any OpenAI-compatible server | `/v1/chat/completions` | server running locally |

Agents created through Codex show up in the ChatGPT desktop app's Codex history, and Antigravity agents in Antigravity's history.

## Install

Needs Python 3.11+ and [uv](https://docs.astral.sh/uv/) (or pip).

```powershell
uv tool install git+https://github.com/s0wingseason/agent-fleet-mcp
agent-fleet install      # adds agent-fleet to every supported app it finds
agent-fleet doctor       # shows which apps and backends were found
```

Then fully quit and reopen each app (Claude Desktop and ChatGPT keep running in the tray).

`agent-fleet install --hosts claude-desktop,codex` limits it to some apps; `--dry-run` shows what would change. `agent-fleet uninstall` removes it again. Every edited config file gets a one-time `*.agent-fleet.bak` backup.

Apps it configures: Claude Desktop (including the Microsoft Store build), Claude Code, ChatGPT desktop / Codex CLI (`~/.codex/config.toml`), Google Antigravity (`~/.gemini/.../mcp_config.json`), Gemini CLI, VS Code, Cursor, Windsurf, LM Studio, Hermes Agent.

Not MCP hosts, so nothing is installed into them: Ollama, vLLM (use them as agent backends). Bionic and Open WebUI need manual setup (not covered yet).

## Tools

| Tool | Who |
|---|---|
| `fleet_status` | everyone |
| `spawn_agent`, `send_message`, `wait_for_agents`, `get_agent`, `get_transcript`, `interrupt_agent`, `stop_agent` | lead: any agent; manager: its own children |
| `send_files`, `fetch_files`, `list_files` | same; files go through each agent's `inbox` / `outbox` |
| `list_app_conversations`, `read_app_conversation`, `adopt_conversation` | lead only — reads Codex, Claude Code and Antigravity history saved on this PC, or takes over one of those conversations |

Typical lead prompt: *"Use agent-fleet: spawn a codex manager to refactor X, an antigravity manager to write tests, wait for both, then fetch their files."*

Calls never block more than ~55 s (MCP hosts time out around 60 s); poll `wait_for_agents` for long jobs.

## How it works

- Every app launches its own `agent-fleet serve` process. Agents run inside the process that created them; all processes share one SQLite database in `~/.agent-fleet`, so every app sees every agent, its status and transcript. An agent can only be messaged from the app that runs it — unless that app has closed, in which case the next app to message it takes it over and resumes the same conversation.
- Managers get the agent-fleet tools injected (Codex via `-c mcp_servers...`, Claude Code via `--mcp-config`) with their own identity; children run without them.
- Folders: `~/.agent-fleet/agents/<id>/{work,inbox,outbox}`, shared drop folder `~/.agent-fleet/exchange`. Config: `~/.agent-fleet/config.toml` (models, sandbox modes, limits, allowed folders).

## Known limits of this beta

- Not yet tested end to end on Windows with all backends together; please file issues.
- No Windows `.exe` installer yet (planned), and no desktop/UI automation.
- Regular ChatGPT chats (not Codex) live in OpenAI's cloud and aren't readable here. Automating the ChatGPT window to scrape replies conflicts with OpenAI's Terms of Use; this project only uses OpenAI's own `codex app-server` interface.
- Antigravity history reading is best-effort (undocumented storage format).
- Local-model agents are chat-only (no file tools yet).
- If `codex` on your PATH is a different program (e.g. the PyPI "codex" comic server), agent-fleet ignores it and looks for OpenAI's CLI under npm's global folder; `agent-fleet doctor` reports this.

## Safety

Agents run with your user account. Codex agents default to `workspace-write` sandbox; Claude Code agents to `acceptEdits`; Antigravity agents to `--sandbox`. File tools refuse paths outside allowed folders and anything that looks like keys or credentials. Review `~/.agent-fleet/config.toml` before giving agents broader access.

MIT licensed.

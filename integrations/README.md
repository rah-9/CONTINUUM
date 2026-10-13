# Continuum as a plugin for coding agents (MCP)

Continuum ships a **local stdio MCP server** (`continuum mcp`). Any MCP-capable agent can use it
to delegate pre-registered tasks and follow them. Progress stays durable and verified,
external effects are never repeated, and a run can continue on a different model from
verified state.

| Tool | Kind | Purpose |
|---|---|---|
| `list_tasks` | read | pre-registered tasks |
| `run_task(task_id, model_alias?)` | delegate | run until complete or paused |
| `resume_run(run_id, model_alias?)` | delegate | continue, optionally on another model |
| `cancel_run(run_id, reason)` | delegate | stop new effects |
| `get_status(run_id)` | read | milestones, actions, usage |
| `inspect_run(run_id)` | read | side-effect-free timeline |
| `pending_decisions(run_id)` | read | decisions only a human may make, with the command |
| `list_runs(limit?)` | read | recent runs |

**Human decisions are intentionally not tools.** These include approving a gated action, confirming an
uncertain effect, `retry_at_risk` and `abort`. A model must not approve its own side effects. The
agent shows the user the exact `continuum resolve …` command instead.

## 1. Install once

```bash
git clone https://github.com/rah-9/CONTINUUM && cd CONTINUUM
uv venv -p 3.12 .venv && uv pip install -p .venv -e ".[mcp]"
export PATH="$PWD/.venv/bin:$PATH"            # or use the absolute path to .venv/bin/continuum
export CONTINUUM_DATABASE_URL=postgresql://continuum:continuum@localhost:5432/continuum
continuum doctor                               # checks DB, schema, tasks and model profiles
```

The server creates its tables on start. Set `CONTINUUM_CONFIG_DIR` to this repo's `config/`
directory when the agent launches the server from a different working directory.

## 2. Connect your agent

- **Claude Code (plugin + skill):**
  `/plugin marketplace add rah-9/CONTINUUM`, then `/plugin install continuum@continuum`.
  For the MCP server alone, run `claude mcp add continuum -- continuum mcp`.
- **Cursor:** copy `cursor/mcp.json` to `.cursor/mcp.json` (project) or `~/.cursor/mcp.json`.
- **VS Code (Copilot agent mode):** copy `vscode/mcp.json` to `.vscode/mcp.json`.
- **OpenAI Codex CLI:** append `codex/config.toml` to `~/.codex/config.toml`.
- **Claude Desktop:** merge `claude-desktop/claude_desktop_config.json` into its config.
- **Any other MCP client:** run the stdio command `continuum mcp` with the env vars above.

Replace the `/absolute/path/...` placeholders with your paths.

## 3. Try it

Ask your agent: *"Use Continuum to run `demo_opaque_needs_human`, then tell me what decision
is pending."* The run pauses because an opaque send's outcome is unknown. The agent calls
`pending_decisions` and hands you the `continuum resolve` command. After you run it, the agent
calls `resume_run`, and the run completes with exactly one message sent.

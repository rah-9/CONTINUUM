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
| `coding_resume(repo_path, agent)` | coding | **call first in every chat**: join the repo's session, get the brief |
| `coding_start(repo_path, goal, agent, tasks?)` | coding | start a session when none exists |
| `coding_brief(session_id \| repo_path)` | read | verified facts, tasks, notes, rules |
| `coding_add_task` / `coding_set_task_status` | coding | plan and track work |
| `coding_note(kind, text)` | coding | decision, context, blocker or next_step |
| `coding_checkpoint(summary)` | coding | snapshot; Continuum observes the code itself |
| `coding_run_check(name)` | coding | run a **human-registered** check, record the result |
| `coding_complete_task(task_id)` | coding | refused unless its checks pass on the current code |
| `coding_sessions` / `coding_close` | coding | list or close sessions |

**Coding continuity** lets you switch models mid-work (Opus 4.8 → 5.5, GPT-5.6 → 6.1, any vendor, any
new chat) and keep the coding context. See [`docs/CODING-HANDOFF.md`](../docs/CODING-HANDOFF.md).
Put [`AGENT_RULES.md`](AGENT_RULES.md) in your repo's `AGENTS.md` or `CLAUDE.md` so every model follows the same
protocol. Checks are registered only by a person: `continuum coding checks add . tests --actor you -- pytest -q`.

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

## 4. In the cloud (Claude Code on the web, Codex cloud) and in Codex

`continuum mcp` is always a **local child process of the agent**. In the cloud, "local" means
inside the agent's container. Nothing listens on a network port, and there is no Continuum server to host.
Two things make it work across ephemeral containers:

1. **A PostgreSQL that outlives the container.** Use any hosted PostgreSQL 14+ (Neon, Supabase,
   RDS, Cloud SQL, your own). Put its URL in the environment's **secrets / environment variables**
   as `CONTINUUM_DATABASE_URL`, never in git. Make sure the environment's network policy allows that
   host. Each new container (and each new model) then finds the same coding session by repository.
2. **Continuum installed by the setup script.** Use [`cloud/setup.sh`](cloud/setup.sh) as, or inside, the
   environment's setup script. It installs `continuum[mcp]` and runs `db-init`.

Then connect the agent:

- **Claude Code (local or on the web):** commit [`claude-code/project.mcp.json`](claude-code/project.mcp.json)
  as `.mcp.json` at your repo root. Claude Code starts `continuum mcp` for that project, with the URL
  taken from the environment. Add the rules to `CLAUDE.md`.
- **Codex CLI:** add [`codex/config.toml`](codex/config.toml) to `~/.codex/config.toml` and the rules to
  `AGENTS.md`. Switching `gpt-5.6` → `gpt-6.1` (`/model`, or a new `codex` session) needs nothing more.
  The new model calls `coding_resume` because `AGENTS.md` tells it to.
- **Codex cloud and other agents without MCP:** the rules work through the shell instead
  (`continuum coding resume --repo . --agent <model>`, etc.). The protocol and the database are the same.

**Limit:** sessions are keyed by the repository's absolute path, and Continuum observes the code at
that path. Cloud environments normally check a repo out at the same path every time, which works. If
your checkout path changes between containers, keep it stable (for example with a symlink). Keying by
git remote is planned (docs/PROJECT-JOURNAL.md §5).

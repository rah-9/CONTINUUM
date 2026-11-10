# Installing Continuum

Written so that a person **or a coding agent** can follow it start to finish. To let your
agent do it, say:

> Install the Continuum MCP server from https://github.com/rah-9/CONTINUUM by following
> its INSTALL.md, then check it with `continuum doctor`.

Every step is a command the agent can run; every step can be checked. Nothing here
spends money or calls a paid model.

## What you get

- an MCP server (`continuum mcp`, local stdio) with the `coding_*` tools: `coding_resume`
  gives any new chat, model or app the session's memory; `coding_recall` searches it
- hooks that journal what you ask and what the agent does (Claude Code, Codex, Gemini
  CLI, Cursor), so the memory exists even if the agent never writes notes
- the session memory: Continuum's own digest (no model, offline, free) by default, or a
  free model when you give it an OpenRouter key

## 1. Requirements

| Need | Check | Get it |
|---|---|---|
| Python 3.12+ | `python3 --version` | https://www.python.org/downloads/ |
| `uv` (or `pipx`) | `uv --version` | `curl -LsSf https://astral.sh/uv/install.sh \| sh` (Windows: `winget install astral-sh.uv`) |
| PostgreSQL 14+ | `pg_isready` or `docker ps` | step 2 |
| `git` | `git --version` | your package manager |

## 2. A database

Continuum keeps everything in PostgreSQL. It is the only service; there is no Continuum
server to run. If you have one, skip to step 3. Otherwise, with Docker:

```bash
docker run -d --name continuum-pg --restart unless-stopped -p 5432:5432 \
  -e POSTGRES_USER=continuum -e POSTGRES_PASSWORD=continuum -e POSTGRES_DB=continuum \
  postgres:16
```

Without Docker: install PostgreSQL 16 from your package manager (`brew install
postgresql@16`, `sudo apt install postgresql`) and create a user and database named
`continuum`.

Then put the address in your shell profile (`~/.zshrc`, `~/.bashrc`; on Windows, the
user environment variables) so every app sees it:

```bash
export CONTINUUM_DATABASE_URL=postgresql://continuum:continuum@localhost:5432/continuum
```

Tables are created automatically on first use.

## 3. Install the command

```bash
uv tool install "continuum[mcp] @ git+https://github.com/rah-9/CONTINUUM"
continuum doctor
```

`continuum doctor` must print `"ok": true`. If `database` is not ok, its `hint` says what
to do (usually: start PostgreSQL, or fix `CONTINUUM_DATABASE_URL`). `memory` shows which
memory the brief will use. `live_models` is only for delegating pre-registered tasks and
can be ignored.

To upgrade later: `uv tool upgrade continuum` (see `docs/UPGRADING.md`).

## 4. Connect it to your app

**Claude Code, the whole plugin** (MCP server + capture hooks + the `/resume` and
`/handoff` commands). Inside Claude Code:

```
/plugin marketplace add rah-9/CONTINUUM
/plugin install continuum@continuum
```

**Claude Code, the MCP server only** (one command, for every project):

```bash
claude mcp add continuum --scope user \
  -e CONTINUUM_DATABASE_URL="$CONTINUUM_DATABASE_URL" -- continuum mcp
```

**Per repository** (rules in `AGENTS.md` and `CLAUDE.md`, `.mcp.json`, and capture hooks
for Claude Code, Codex, Gemini CLI and Cursor, each in its own format). Run in the
repository and commit the files:

```bash
continuum coding setup .
```

**Codex, Cursor, VS Code, Windsurf, Gemini CLI and others:** `continuum coding setup .`
prints the snippet for Codex's `~/.codex/config.toml`; the others are in
[`integrations/README.md`](integrations/README.md).

## 5. Check it works

1. Restart the app (MCP servers load at start).
2. In Claude Code, `/mcp` lists `continuum` as connected.
3. Ask the agent: "call coding_resume for this repository". It returns the brief: what
   you asked, what was done, the session's memory, and what to do next.
4. Optional: `continuum coding brief --repo .` prints the same brief in the terminal.

## 6. Optional: a model for the memory

Without any setting, the memory is Continuum's own **session digest**: no model, nothing
leaves your machine, nothing to pay. To have a model write the memory instead:

| Choice | Setting (environment) | Cost |
|---|---|---|
| free OpenRouter models | `OPENROUTER_API_KEY=...` | free (`:free` models only; paid ones are refused unless `CONTINUUM_MEMORY_ALLOW_PAID=1`) |
| a local model server (llama.cpp, LM Studio, Ollama, vLLM) | `CONTINUUM_MEMORY_MODEL=openai-compatible:<model>` and `CONTINUUM_MEMORY_API_BASE=http://localhost:8080/v1` | free, offline |
| no model | `CONTINUUM_MEMORY_MODEL=digest` (the default without a key) | free, offline |

**Optional: understanding by meaning.** An embedding model (not a language model: local,
free, writes nothing) lets the digest rank what answers your request and drop
paraphrases:

```bash
uv tool install --force "continuum[mcp,semantic] @ git+https://github.com/rah-9/CONTINUUM"
export CONTINUUM_EMBED_MODEL=sentence-transformers/all-MiniLM-L6-v2   # downloaded once
```

Keep keys in the environment or your app's secret settings, never in a file in a
repository. `continuum doctor` shows the mode and whether the key is set (never the key).

## Troubleshooting

| Symptom | Fix |
|---|---|
| `continuum: command not found` | `uv tool update-shell`, then open a new terminal |
| doctor: `database` not ok | start PostgreSQL (`docker start continuum-pg`); check `CONTINUUM_DATABASE_URL` |
| `/mcp` does not list `continuum` | restart the app; `claude mcp list`; the command must be on `PATH` for the app |
| the brief is empty in a new chat | hooks are not installed: use the plugin or `continuum coding setup .` |
| memory: `rate or daily limit reached` | a free tier's limit: the previous memory stays; it continues later |
| hooks slow or failing | they never block the app: see `$CONTINUUM_HOME/hooks.log` (default `~/.continuum`) |

To remove: `/plugin uninstall continuum` or `claude mcp remove continuum --scope user`,
then `uv tool uninstall continuum`. Your data stays in PostgreSQL until you drop it
(`continuum coding forget` erases a session).

---

_Continuum is proprietary and confidential. © 2026 the Continuum copyright holder. All rights reserved. See [LICENSE](LICENSE)._

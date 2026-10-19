# Continuum

Continuum keeps an AI agent's work **outside the chat**, so you can switch models, versions, vendors or chats without losing it and without paying for the work twice. It does two things:

1. **Coding continuity.** Switch from Opus 4.8 to Opus 5.5, from GPT-5.6 to GPT-6.1, from Claude Code to Codex, or from your laptop to a cloud container, mid-task. The next model gets:
   - **what you asked**, in your own words, from every earlier chat and app
   - what is **proven** done (checks that passed on the current code)
   - **where** the session's code is
   - what was **decided**, and the last agent's report
   - what is **missing or changed** since

   It does not need to re-read the codebase. This works even if the previous agent never used Continuum and its chat ended without warning, because your app's hooks record the conversation.
2. **Runtime continuity.** Long-running agent tasks keep their verified milestones and a ledger of external effects. After a crash, timeout or model switch, valid work is reused, and an uncertain side effect is reconciled **before** any new write. Nothing is sent twice.

Continuum itself uses **no LLM**. Memory is your own words (captured by your app's hooks), what the working agent records (decisions, next steps) and facts Continuum observes (git, check results). Plain code assembles them, so there is no extra model cost. The algorithm and its scenario matrix: [`docs/MEMORY-ALGORITHM.md`](docs/MEMORY-ALGORITHM.md).

> Docs: [how well the memory works](docs/MEMORY-QUALITY.md) · [the memory algorithm](docs/MEMORY-ALGORITHM.md) · [how it runs and connects](docs/OPERATIONS.md) · [coding handoff guide](docs/CODING-HANDOFF.md) · [real handoff trials](docs/HANDOFF-TRIALS.md) · [verification](docs/VERIFICATION.md) · [specification](docs/00-README.md) (read in order) · [decisions](docs/DECISIONS.md) · [changelog](docs/CHANGELOG.md) · [project journal](docs/PROJECT-JOURNAL.md) · [agent contract](AGENTS.md)

## Measured with real models

These are Sonnet → Opus handoffs on a copy of this repository, judged by hidden acceptance tests. Model A built and committed Part 1. Two of the user's requirements existed **only in A's chat**. Model B was told only *"continue where we left off"* ([details](docs/HANDOFF-TRIALS.md)):

| Model B (Opus) | correct result | chat-only requirements kept | work tokens* | files read |
|---|---|---|---|---|
| **with Continuum** (2 runs) | **4/4 both runs** | **2/2** | **~14–17k** | 3–4 |
| without (2 runs) | 1/4 both runs | 0/2 | ~21–29k | 12–14+ |

\*Total tokens minus the ~54k that any agent spends before doing anything.

**A 30-message chat** (v0.9.0):
- Model A worked in real Claude Code with only the hooks; requirements for the remaining work were stated in messages 8, 15, 21 and 27.
- Haiku, Sonnet and Opus each continued from it. Each scored **13/13 with Continuum and 9/13 without**.
- In a benchmark of chats with 10–160 messages, **100%** of planted requirements, changes of mind and answers reach the next model, against 15% for v0.8.0 at 40 messages. The brief costs 0.7k–3k tokens. Details: [`docs/MEMORY-QUALITY.md`](docs/MEMORY-QUALITY.md).

**Switching apps after an abrupt end** (v0.8.0): model A in real Claude Code never used Continuum (only its hooks were installed), and its chat ended mid-task. Model B was configured like another app (rules + MCP, no hooks). B finished it **4/4, with both chat-only requirements**, in 1.8 min. The baseline scored 1/4 in 5.1 min.

Without Continuum, Opus never learned that Part 2 existed. Reaching a correct result would take another explanation turn plus the work itself: about 35–45k work tokens, against ~14k with Continuum.

The trials also found three defects in Continuum, all fixed:
- every `continuum` in paths and commands was redacted, because the default DB password is that word
- decision reasons were clipped mid-sentence
- every commit falsely flagged verified work as changed

## Quickstart

**The only service is PostgreSQL 14+.** There is no Continuum server to run. The CLI, SDK and MCP server are short-lived processes that share the database.

```bash
# 1. PostgreSQL (local, Docker, or hosted)
docker run -d --name continuum-pg -p 5432:5432 -e POSTGRES_USER=continuum \
  -e POSTGRES_PASSWORD=continuum -e POSTGRES_DB=continuum postgres:16
export CONTINUUM_DATABASE_URL=postgresql://continuum:continuum@localhost:5432/continuum

# 2. Install (Linux, macOS, Windows) and check
uv tool install "continuum[mcp] @ git+https://github.com/rah-9/CONTINUUM"   # or: pipx install …
continuum doctor

# 3. Once per repository: rules (AGENTS.md + CLAUDE.md), Claude Code hooks, .mcp.json
continuum coding setup .
#    and the commands that prove work is done (only a human registers these)
continuum coding checks add . tests --actor you -- pytest -q

# 4. Codex: add the snippet `setup` printed to ~/.codex/config.toml (MCP + notify hook).
#    Cursor, VS Code, Windsurf, Gemini CLI, Cline, Claude Desktop: integrations/README.md
```

Tables are created automatically on first use.

## Coding continuity: what the next model gets

The agent calls `coding_resume(repo_path, agent=<model id>)` first in every chat. In Claude Code the brief is also put into the chat by a hook before the first turn. It is a **compact brief**: Markdown plus the task ids it needs, about 1–3k tokens however long the session is. Pass `detail="full"` for the JSON; `coding_recall(query)` searches everything older.

| In the brief | Source | Trust |
|---|---|---|
| **What the user asked**, verbatim, from every chat and app | captured by the app's hooks | the user's words; newest wins |
| Attention: gaps and changes (no capture, uncheckpointed code, failing checks, moved session) | computed | fact |
| Code now: branch, HEAD, changed files, content fingerprint | observed at resume | fact |
| **Code touched in this session:** files, +/- lines, functions defined | observed from git since the session started | fact |
| Commits since the last checkpoint | observed from git | fact |
| Check results, valid only if they ran on the current code and definition | `CheckRan` evidence | fact |
| Tasks, verified or not, and whether any need re-verification | evidence vs the current code | fact |
| Decisions, context, blockers, next steps, checkpoint summaries, the last agent report | written by the previous agent | claim, kept whole |

**"Done" means proven.** `coding_complete_task` is refused unless the task's human-registered checks passed **on the current code**. The fingerprint is the *content* of the working tree, not its commit history, so committing doesn't count as a change and editing does. Agents cannot register or change checks. Secrets are redacted before storage. The full guide is [`docs/CODING-HANDOFF.md`](docs/CODING-HANDOFF.md).

```bash
continuum coding brief --repo .                 # what the next model will see
continuum coding resume --repo . --agent gpt-6.1   # agents without MCP use the same protocol
```

## Runtime continuity

```bash
continuum run demo_ack_loss_recovery       # store commits, ack is lost: proven by query, never re-sent
continuum run demo_opaque_needs_human      # opaque send times out: pauses for a human
continuum resolve <run_id> <action_id> confirmed --actor you --reason "seen" --evidence receipt:123
continuum resume <run_id> --model anthropic/claude-opus-5-5   # continue on another model or vendor
continuum inspect <run_id>                 # side-effect-free timeline and projection check
```

```python
from continuum.interfaces.sdk import Continuum

with Continuum() as c:
    result = c.start("upload_and_notify")
    print(result.status, c.status(result.run_id)["milestones"])
```

These invariants hold everywhere (`AGENTS.md`):
- Only the gateway dispatches tools.
- Only verifiers mark milestones verified.
- Each state change is one event plus its projection, in one transaction.
- An ambiguous write is `unknown`, never "failed".
- No new write happens while an effect is unresolved.
- Human decisions (approve, confirm, retry, abort) are never MCP tools.

## Works with any agent, IDE and environment

| | |
|---|---|
| **MCP clients** | Claude Code (plugin + skill + hooks), Cursor, VS Code, Codex CLI, Windsurf, Gemini CLI, Cline, Claude Desktop, JetBrains, Zed: one stdio command, `continuum mcp` |
| **Chat capture** | Claude Code hooks, Codex `notify`, any app with JSON hooks (`continuum hook <app>`); others: `continuum coding import <transcript>` |
| **Another checkout** | a subdirectory, a re-clone, a cloud container: found by git top level, then by remote + branch |
| **Agents without MCP** | the same protocol via the CLI (`continuum coding …`) |
| **OS** | Linux, macOS, Windows. Output is UTF-8 whatever the console encoding. Check commands are resolved like a shell would (`npm` → `npm.cmd`) |
| **Cloud** | `integrations/cloud/setup.sh` installs through `uv tool` with its own Python 3.12 (PEP 668-safe). Point `CONTINUUM_DATABASE_URL` at a hosted PostgreSQL so sessions outlive containers |
| **Launch directory** | state lives in `~/.continuum`, never in the directory an IDE starts the server in |

The 20 MCP tools are:
- **tasks:** `list_tasks`, `run_task`, `resume_run`, `cancel_run`, `get_status`, `inspect_run`, `pending_decisions`, `list_runs`
- **coding:** `coding_resume`, `coding_start`, `coding_brief`, `coding_add_task`, `coding_set_task_status`, `coding_complete_task`, `coding_note`, `coding_checkpoint`, `coding_run_check`, `coding_recall`, `coding_sessions`, `coding_close`

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `CONTINUUM_DATABASE_URL` | `postgresql://continuum:continuum@localhost:5432/continuum` | the database (the only service) |
| `CONTINUUM_DB_SCHEMA` | `public` | schema; lower-case identifier |
| `CONTINUUM_HOME` | `~/.continuum` | run workspaces and mock-service state |
| `CONTINUUM_CONFIG_DIR` | `./config` if it has `tasks/`, else `~/.continuum/config` | pre-registered tasks, `models.yaml` |
| `CONTINUUM_CODING_ROOTS` | unset | restrict coding sessions to these directories (`:`-separated; `;` on Windows) |
| `CONTINUUM_DB_TIMEOUT_SECONDS` | `10` | how long a command waits for the database |
| `CONTINUUM_OWNER_ID` | `ctl-<host>-<pid>-<random>` | lease identity; must be unique per process if set |
| `CONTINUUM_HOOKS` | on | `off` disables app hooks (chat capture, session-start brief) |

API keys come only from the environment. They are redacted before anything is stored, as are URL passwords that look like secrets and well-known token formats pasted into chat. Full reference: [`docs/OPERATIONS.md`](docs/OPERATIONS.md).

## Verified

350 tests across these layers:
- unit
- integration
- contract (OpenAI and Anthropic request rules)
- property (Hypothesis stateful)
- chaos (crash at every commit boundary, real process races)
- non-functional (locks, secrets, determinism, concurrency, performance)
- three audit regression suites
- portability
- brief quality
- continuity scenarios (app switch, another checkout, abrupt end, long sessions, secrets in chat; see [`docs/MEMORY-ALGORITHM.md`](docs/MEMORY-ALGORITHM.md) §5)
- long-chat memory quality (planted requirements in chats of 10–160 messages, pinned at ≥95% recall and zero noise)

Every requirement maps to tests in [`tests/traceability.yaml`](tests/traceability.yaml), and every defect found has a regression test. Results are in [`docs/VERIFICATION.md`](docs/VERIFICATION.md).

```bash
make check                                  # ruff + mypy --strict + all tests (needs PostgreSQL)
continuum-lab run --suite safety --out results/safety    # B0 vs Continuum, 912 paired cells
eval/handoff_trials/export_feature/judge.sh <project>    # judge a real model-switch trial
```

## Repository layout

```text
src/continuum/
  core/          runtime, store (events + projections), leases, ledger, reconciliation,
                 verification, sessions/handoff, budgets, redaction, schema.sql
  coding/        coding continuity: service, repo observer (content fingerprint, code
                 map), brief, check runner
  models/ tools/ verify/ interfaces/ (SDK, CLI, loopback HTTP, MCP)
eval/
  continuum_lab/   B0-vs-Continuum fault-injection lab
  handoff_trials/  real model-switch trials (setup, prompts, hidden tests, judge)
integrations/    AGENT_RULES.md, Claude Code plugin, configs for Cursor, VS Code, Codex,
                 Windsurf, Gemini CLI, Cline, Claude Desktop; cloud/setup.sh
docs/            00–07 spec, OPERATIONS, CODING-HANDOFF, HANDOFF-TRIALS, VERIFICATION,
                 MODEL-SWITCHING, DECISIONS, CHANGELOG, PROJECT-JOURNAL
tests/           unit/ integration/ contract/ property/ chaos/ nonfunctional/
```

## Development

Each change follows [`docs/06-DEV-WORKFLOW.md`](docs/06-DEV-WORKFLOW.md):
1. One narrow change per branch.
2. Spec and docs first; contract changes update every affected doc and the changelog.
3. A failing test first for ledger, state or recovery behaviour.
4. `make check` locally.
5. CI runs the same gates against PostgreSQL 16, plus a lab smoke run.

Results are reported honestly: unknown cost is never zero, pauses are not successes, and fake-model results are not presented as live evidence.

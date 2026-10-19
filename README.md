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

## What has been measured

Every number below comes from a test or a trial in this repository, with the method
written down so it can be rerun. Details: [real model trials](docs/HANDOFF-TRIALS.md) and
[how well the memory works](docs/MEMORY-QUALITY.md).

### Real model switches

**Setup.**
- Model A works and then stops.
- Model B, in a brand-new chat, is told only *"continue where we left off"*.
- Hidden acceptance tests that neither model sees judge the result.
- The hard part is always the same: some of the user's requirements exist **only in A's chat**.

| Trial | Model A | Model B | with Continuum | without |
|---|---|---|---|---|
| 1. Easy handoff (everything is in the code) | Sonnet | Opus | 7/7 | 7/7 |
| 2–3. Requirements only in chat | Sonnet, records notes | Opus (2 runs) | **4/4, 4/4** | 1/4, 1/4 |
| 4. App switch after an abrupt end | Sonnet in Claude Code, **never used Continuum** (hooks only) | Opus, set up like another app | **4/4** in 1.8 min | 1/4 in 5.1 min |
| 5. **A 30-message chat**, requirements in messages 8, 15, 21 and 27 | Sonnet in Claude Code, 30 turns, hooks only | Haiku (2 runs) | **13/13, 13/13** | 9/13 |
| | | Sonnet | **13/13** | 9/13 |
| | | Opus | **13/13** | 9/13 |
| 7. **What the agent did** (capture fidelity): tests, an edit, a script, a subagent, a skill, an MCP ticket | Sonnet in Claude Code, hooks only | Haiku / Sonnet / Opus: "what did it do?" | **4/4** each, ~half the cost; 10/10 tool calls and 3/3 file changes captured | 3/4 each (the MCP ticket id was unknowable) |
| 6. **Debugging handoff**: A found the root cause; the user's extra facts and constraints were only in the chat | Sonnet in Claude Code, 3 turns, hooks only, no code changed | Haiku / Sonnet / Opus | **7/7** each | 4/7 each |

**What the baselines did.**
- **Without Continuum:** every B kept what was already in the code. None of them learned what was still to do, and each said it could not tell what was planned.
- **With Continuum:** every B followed the repository's rules on its own (`coding_resume` first) and finished the job.

**Cost.** In Trial 5, model A's chat had reached **~135k tokens per call** by message 30. The
Continuum brief carried the same requirements in **~2.1k tokens**. In Trials 2–3, Continuum's
B used ~14–17k work tokens and read 3–4 files. Without it, B used ~21–29k, read 12+ files
and still delivered the wrong thing.

### Memory, measured without a model

| Question | Result |
|---|---|
| Does what the user said **anywhere in a long chat** reach the next model? (benchmark, 20 chats per length, requirements, changes of mind, answers and decisions planted at random turns, held-out phrasings) | **100%** of every kind at 10, 20, 40, 80 and 160 messages, with **0** pasted-log noise. The v0.8.0 brief kept 15% at 40 messages and 4% at 160. |
| Is **everything the agent did** captured? (real Claude Code, compared call by call with its own transcript) | **10/10** tool calls with correct results, including a subagent's call, a skill and an MCP call with its result; **3/3** changed files, including one written by a script; hooks take a median of ~100 ms |
| Can a tool's full output be found later? (benchmark: an error code planted mid-way in a 1,500-line output) | **100%**, from the stored output; a replaced value is never shown as current (100%); requirement lines are the user's exact words (100%) |
| How big is the brief? | 0.8k tokens at 10 messages, 1.2k at 40, 1.8k at 80, 3.0k at 160 |
| Are **both sides** of the chat kept exactly? (the real 30-message transcript replayed through Claude Code's hook sequence) | your messages **30/30**, Claude's messages **47/47**: none missing, none duplicated, in order, also when the hooks failed for the first 4 turns or for every turn |
| Does a hook ever break the app? | No. The database was down for the first minutes of Trial 5: model A was unaffected, and every message was recovered from the transcript afterwards. |
| Runtime safety (912 paired fault-injection cells) | **0** duplicate side effects (baseline 140), **0** false completions, **100%** of verified progress kept after crashes (baseline 73%) |

### Defects the trials found (all fixed, each with a regression test)

- every `continuum` in paths and commands was redacted (the default DB password is that word)
- decision reasons were clipped mid-sentence
- every commit falsely flagged verified work as changed
- the brief kept only the newest messages, losing requirements from the middle of long chats
- a second "yes" or "continue" in one chat was dropped
- only Claude's final message per turn was kept; its explanations and caveats were lost
- Claude Code's internal request copies were read as extra messages
- a weaker model read the user's "stop here" (said to the previous model) as its own instruction, and stalled on an old open question
- `continuum … | head` crashed
- a long final message starting with "OK" was shown as the answer to the agent's open question
- a "yes" typed in a second app lost its question, or was dropped as a copy of an earlier "yes" (found by the two-app benchmark)

**Not yet measured:** GPT models (Codex), because the environment these trials ran in has no
OpenAI access. The GPT → Claude runbook is in [`eval/handoff_trials/`](eval/handoff_trials/README.md).

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

The agent calls `coding_resume(repo_path, agent=<model id>)` first in every chat. In Claude Code the brief is also put into the chat by a hook before the first turn. It is a **compact brief**: Markdown plus the task ids it needs, about 1–3k tokens however long the session is (measured: 1.2k at 40 messages, 3.0k at 160). Pass `detail="full"` for the JSON; `coding_recall(query)` searches everything older.

| In the brief | Source | Trust |
|---|---|---|
| **What the user asked**: every requirement, change of mind and answered question from the whole chat, numbered in order; the most recent message whole | captured by the app's hooks, consolidated without a model | the user's words; the higher number wins |
| Attention: gaps and changes (no capture, uncheckpointed code, failing checks, moved session, **calls with unknown outcomes**, recent failures) | computed | fact |
| **What the previous agent did:** every tool call (commands, edits, MCP tools, skills, subagents) with its status, the files each call changed, recent failures with the end of their output, subagent reports; `[#N]` opens the full record with `coding_show` | recorded by the app's hooks, file changes observed with snapshots | fact (outputs are data; subagent reports are claims) |
| Code now: branch, HEAD, changed files, content fingerprint | observed at resume | fact |
| **Code touched in this session:** files, +/- lines, functions defined | observed from git since the session started | fact |
| Commits since the last checkpoint | observed from git | fact |
| Check results, valid only if they ran on the current code and definition | `CheckRan` evidence | fact |
| Tasks, verified or not, and whether any need re-verification | evidence vs the current code | fact |
| Choices and caveats the previous agent reported, and its last message | captured from the agent's own replies | claim |
| Decisions, context, blockers, next steps, checkpoint summaries | written by the previous agent | claim, kept whole |

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

362 tests across these layers:
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
- exact preservation of both sides of the chat through hook outages

Every requirement maps to tests in [`tests/traceability.yaml`](tests/traceability.yaml), and every defect found has a regression test. Results are in [`docs/VERIFICATION.md`](docs/VERIFICATION.md).

```bash
make check                                  # ruff + mypy --strict + all tests (needs PostgreSQL)
continuum-lab run --suite safety --out results/safety    # B0 vs Continuum, 912 paired cells
eval/handoff_trials/export_feature/judge.sh <project>    # judge a real model-switch trial
python -m continuum_lab.memory_bench                      # long-chat memory benchmark
```

## Repository layout

```text
src/continuum/
  core/          runtime, store (events + projections), leases, ledger, reconciliation,
                 verification, sessions/handoff, budgets, redaction, schema.sql
  coding/        coding continuity: service, repo observer (content fingerprint, code
                 map), memory (conversation consolidation), brief, transcript parser,
                 app hooks, setup, check runner
  models/ tools/ verify/ interfaces/ (SDK, CLI, loopback HTTP, MCP)
eval/
  continuum_lab/   B0-vs-Continuum fault-injection lab; memory_bench (long-chat memory)
  handoff_trials/  real model-switch trials: export_feature, long_chat (30 messages),
                   hidden judges, transcript replay
integrations/    AGENT_RULES.md, Claude Code plugin (skill, hooks, commands), configs for Cursor, VS Code, Codex,
                 Windsurf, Gemini CLI, Cline, Claude Desktop; cloud/setup.sh
docs/            00–07 spec, MEMORY-QUALITY, MEMORY-ALGORITHM, OPERATIONS, CODING-HANDOFF,
                 HANDOFF-TRIALS, VERIFICATION, MODEL-SWITCHING, DECISIONS, CHANGELOG,
                 PROJECT-JOURNAL
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

## Releases, security and upgrades

- **Releases** are private: tagging `v<version>` builds the wheel and sdist, checks that the wheel installs and works on its own, and attaches both (with `SHA256SUMS`) to a GitHub release in this repository (`.github/workflows/release.yml`). Nothing is published to a public index.
- **Security:** how to report a vulnerability, supported versions and the security controls are in [SECURITY.md](SECURITY.md).
- **Upgrading:** the schema migrates itself; back up first. See [docs/UPGRADING.md](docs/UPGRADING.md).
- **Several people, one database:** per-person isolation with PostgreSQL roles, plus erasure and retention, in [docs/OPERATIONS.md](docs/OPERATIONS.md).

## License

Proprietary software. All rights reserved. Use, copying and distribution are permitted only under a separate written agreement with the copyright holder ([`LICENSE`](LICENSE), [`NOTICE`](NOTICE)). The package is marked `Private :: Do Not Upload`, so public package indexes refuse it.

---

_Continuum is proprietary and confidential. © 2026 the Continuum copyright holder. All rights reserved. See [LICENSE](LICENSE)._

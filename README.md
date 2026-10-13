# Continuum

Continuum is a model-agnostic **agent continuity runtime**. It keeps a multi-step agent's progress in durable, runtime-owned state instead of in a provider's chat history. That state has three parts:

- verified milestone evidence
- a ledger of external effects
- a deterministic continuation packet

After a crash, timeout or model switch, Continuum reuses work that is still valid. It also reconciles any effect whose outcome is uncertain **before** it allows a new side-effecting call.

> Specification: [`docs/00-README.md`](docs/00-README.md) (read in order). Coding-agent contract: [`AGENTS.md`](AGENTS.md). Decisions: [`docs/DECISIONS.md`](docs/DECISIONS.md). What we decided, did and plan next: [`docs/PROJECT-JOURNAL.md`](docs/PROJECT-JOURNAL.md).

## Stack

Python 3.12, Pydantic v2, **PostgreSQL 14+** (psycopg 3), jsonschema, FastAPI (loopback HTTP), LiteLLM (live providers), the MCP SDK (stdio facade), pytest, hypothesis, ruff and mypy (`--strict`).

## Architecture

```text
SDK / CLI / loopback HTTP / stdio MCP          src/continuum/interfaces/
        │
Run controller + lease (fencing epochs)        core/runtime.py, core/leases.py, core/journal.py
Budgets / model sessions + handoff             core/budgets.py, core/sessions.py
        │
Step loop + packet builder ── ModelAdapter     core/handoff.py, models/
        │
Permission + Tool Gateway ── trusted tools     tools/gateway.py, tools/trusted/
        │
Action ledger ◄── Reconciler                   core/ledger.py, core/reconciliation.py
        │
Milestone verifiers ── evidence/workspace      verify/, core/verification.py, core/evidence.py
        │
PostgreSQL events + projections (1 tx each)    core/store.py, core/projections.py, core/schema.sql
```

These invariants hold everywhere (see `AGENTS.md`):

- Only the gateway dispatches tools.
- Only verifiers mark milestones verified.
- Every state change is one event plus its projection, committed in a single transaction.
- An ambiguous write is `unknown`, never "failed".
- No new write happens while any effect is unresolved.

## Setup

```bash
# 1. PostgreSQL (any local 14+ instance)
sudo -u postgres psql -c "CREATE USER continuum WITH PASSWORD 'continuum';" \
                      -c "CREATE DATABASE continuum OWNER continuum;" \
                      -c "CREATE DATABASE continuum_test OWNER continuum;"

# 2. Python environment
make install                       # uv venv + editable install with all extras
export CONTINUUM_DATABASE_URL=postgresql://continuum:continuum@localhost:5432/continuum
make db                            # create tables (idempotent)
```

Environment variables:

| Variable | Default | Purpose |
|---|---|---|
| `CONTINUUM_DATABASE_URL` | `postgresql://continuum:continuum@localhost:5432/continuum` | runtime database |
| `CONTINUUM_DB_SCHEMA` | `public` | schema (`search_path`) |
| `CONTINUUM_HOME` | `.continuum` | workspaces, mock-service state |
| `CONTINUUM_CONFIG_DIR` | `config` | `models.yaml`, `tasks/*.yaml` |
| `CONTINUUM_TEST_DATABASE_URL` | `…/continuum_test` | disposable test database |

Provider API keys come only from the environment. They are redacted before anything is persisted.

## Usage

```bash
continuum tasks                                   # pre-registered tasks (config/tasks)
continuum run inventory_upload_v1                 # start; prints JSON RunResult
continuum status <run_id>
continuum resume <run_id> --model B               # continue under lease + ledger rules
continuum resolve <run_id> <action_id> confirmed --actor ops --reason "seen" --evidence receipt:123
continuum resolve <run_id> <action_id> approve   --actor lead --reason "reviewed"
continuum inspect <run_id>                        # side-effect-free timeline + projection diff
continuum trace <run_id> --out run.jsonl          # full JSON-lines trace with redacted blobs
continuum --log-level INFO run db_update_selected # stream structured JSON trace to stderr
continuum serve                                   # http://127.0.0.1:8765 (loopback only)
continuum mcp                                     # stdio MCP: run_task, get_status
```

```python
from continuum.interfaces.sdk import Continuum

with Continuum() as c:
    result = c.start("upload_and_notify")
    print(result.status, c.status(result.run_id)["milestones"])
```

### See the core idea in two minutes

```bash
# The store commits the upload but the acknowledgement is lost. Continuum marks the
# attempt `unknown`, proves the effect with an authoritative query, and finishes
# without uploading twice.
continuum run demo_ack_loss_recovery
continuum inspect <run_id>       # timeline: ambiguous outcome -> reconciliation -> verified

# An opaque provider times out after sending. Continuum never re-sends on its own:
continuum run demo_opaque_needs_human                    # -> awaiting_reconciliation
continuum resolve <run_id> <action_id> confirmed --actor you --reason "seen in console" \
    --evidence receipt:123
continuum resume <run_id>                                # -> completed, exactly one send
```

The pre-registered tasks use the deterministic local fake agent by default. To use live models:

1. Fill verified profiles in `config/models.yaml`. Placeholder profiles are rejected at session start.
2. Set `fake_models: false` in the task's YAML.

## Use Continuum from any coding agent (MCP plugin)

`continuum mcp` is a local stdio MCP server, so Claude Code, Cursor, VS Code, Codex, Claude Desktop or any other MCP client can delegate tasks to Continuum. It exposes `list_tasks`, `run_task`, `resume_run` (optionally on another model), `cancel_run`, `get_status`, `inspect_run`, `pending_decisions` and `list_runs`.

Human decisions are deliberately **not** tools. The agent shows you the exact `continuum resolve` command instead. Setup for each agent is in [`integrations/README.md`](integrations/README.md).

```bash
claude mcp add continuum -- continuum mcp                 # Claude Code (server only)
/plugin marketplace add rah-9/CONTINUUM                   # Claude Code plugin + skill
continuum doctor                                          # verify the installation
```

## Development workflow

Each change follows the cycle in `docs/06-DEV-WORKFLOW.md`:

1. **Branch:** one narrow vertical task per branch.
2. **Spec first:** read `AGENTS.md` and the relevant spec. Contract changes update every affected doc, plus `docs/CHANGELOG.md`.
3. **Test first:** for ledger, state or recovery behaviour, write the failing test before the change.
4. **Gate locally:** run `make check`, which is ruff, then mypy `--strict`, then the full pytest suite.
5. **CI:** `.github/workflows/ci.yml` runs the same gates against a PostgreSQL 16 service, plus a Continuum Lab smoke run.
6. **Review:** use the mandatory review questions in docs/06 §5.

### Test layers

| Layer | Path | What it proves |
|---|---|---|
| Unit / property | `tests/unit` | spec validation, transitions, identity, canonical encoding, packet determinism, fingerprints, redaction, adapter normalization, doc/DDL sync |
| Integration | `tests/integration` | atomic events+projections, fencing, leases, gateway dedupe/conflict/permissions, reconciliation table, U1–U7 flows, drift, budgets, replay, SDK/CLI/HTTP/MCP, lab safety gates |
| Contract | `tests/contract` | provider-format tool calls normalized via LiteLLM; error taxonomy drives retry/handoff; live tests opt-in (`CONTINUUM_LIVE=1 pytest -m live`) |
| Chaos | `tests/chaos` | crash at every write-ahead/commit boundary, then resume; real two-process lease races; `os._exit` after remote commit; stale owner returning |

## Continuum Lab

Continuum Lab is the in-repo evaluation suite (`eval/continuum_lab`). It compares **B0**, a plain tool loop with retries and transcript handoff, against **C1** (Continuum). Both get the same tasks, tools, permissions, budgets and ground-truth scorer:

```bash
continuum-lab run --suite smoke  --out results/smoke
continuum-lab run --suite safety --out results/safety          # 15 tasks × 6 faults × 3 modes × 2 seeds
continuum-lab run --tasks inventory_upload_v1 --faults commit_then_drop --modes observable
```

Each run writes `results.json` (raw cells) and `report.md`. The report includes Wilson intervals, matched paired-bootstrap differences, a per-fault breakdown and limitations.

Fake-model results only test the modeled safety properties. They are not evidence about live providers.

## Repository layout

```text
AGENTS.md  README.md  Makefile  pyproject.toml
config/            models.yaml (live profiles), tasks/*.yaml (pre-registered tasks)
docs/              00–07 spec, DECISIONS.md, CHANGELOG.md, REVIEW-CHANGES.md
src/continuum/     core/  models/  tools/  verify/  interfaces/
integrations/      MCP client configs: Claude Code plugin, Cursor, VS Code, Codex, Claude Desktop
eval/continuum_lab mock_services/  tasks/  baseline.py  fault_injection.py  runner.py  metrics.py  report.py
tests/             unit/  integration/  contract/  chaos/
```

# Handoff trials

Real model-switch trials. Model A starts a coding task through Continuum, then stops. Model B
continues in a fresh chat, either with Continuum or without it (the baseline). The result is
judged by tests the agents never see. Results and method: [`docs/HANDOFF-TRIALS.md`](../../docs/HANDOFF-TRIALS.md).

`export_feature/` is the realistic trial:
- Model A builds and **commits** Part 1.
- The user's chat holds two requirements for Part 2 that never appear in the code: "never overwrite without `--force`" and "sorted keys".
- Model B is told only *"Continue where we left off on the session-export feature."*

| File | Purpose |
|---|---|
| `setup.sh WORKDIR` | copies this repo's HEAD into `WORKDIR/proj` (without the trial files), commits a baseline, registers checks `e1` and `e2` |
| `prompt_A.md`, `prompt_B_continuum.md`, `prompt_B_baseline.md` | the exact prompts; fill in `{PROJ}`, `{PROJ_B0}` and `{AGENT_ID}` |
| `accept_e1_function.py`, `accept_e2_cli.py` | the hidden acceptance tests, registered as the human checks |
| `judge_chat_requirements.py` | the hidden judge for the two chat-only requirements |
| `judge.sh PROJECT_DIR` | runs all of them against a finished project (`4 passed` = complete and faithful) |

## Run it

You need PostgreSQL, `continuum` on PATH, and `CONTINUUM_DATABASE_URL` (the journal). The
tests create throwaway schemas in `TRIAL_DATABASE_URL`, which defaults to the local
`continuum_test` database. `TRIAL_PYTHON` must be a Python with Continuum's dependencies.

```bash
export TRIAL_PYTHON=$PWD/.venv/bin/python CONTINUUM_DB_SCHEMA=handoff_trial
eval/handoff_trials/export_feature/setup.sh /tmp/trial
# 1. model A (prompt_A.md) in /tmp/trial/proj; it commits Part 1 and stops
cp -a /tmp/trial/proj /tmp/trial/proj_b0          # same starting point for both B runs
# 2. model B with Continuum (prompt_B_continuum.md) in /tmp/trial/proj
# 3. model B baseline (prompt_B_baseline.md) in /tmp/trial/proj_b0, with no Continuum configured
eval/handoff_trials/export_feature/judge.sh /tmp/trial/proj
eval/handoff_trials/export_feature/judge.sh /tmp/trial/proj_b0
```

Record each B run's token usage. Subtract the cost of an idle agent of the same kind (one
told only to reply "OK") to get the work tokens.

## App switch after an abrupt end (Trial 4)

The same project and judge, but model A never uses Continuum: install only the Claude Code
hooks (`integrations/claude-code/hooks/hooks.json` as `.claude/settings.json`), give A the
user's request in two `claude -p` turns (`--resume` for the second, which adds the two
Part 2 requirements and ends the chat), then run `continuum coding setup` for B. Run B with
`CONTINUUM_HOOKS=off` (no capture, no brief at start: like an app without hooks) and
`--mcp-config proj/.mcp.json`. Before the B runs, move A's directory under
`~/.claude/projects/` away, so no B can read A's chat from disk.

## Debugging handoff (Trial 6)

`debug_handoff/run.sh WORK WRAPPER a` runs model A (Sonnet, three turns, hooks only) on the
`invoicer` fixture, saves A's journal with `pg_dump`, and moves A's transcript out of reach.
`run.sh WORK WRAPPER b MODEL continuum|baseline` then runs one model B in its own checkout and
schema, and judges it with `judge.py` (hidden; 7 criteria). WRAPPER runs `claude` in a clean
environment and passes `VAR=value` arguments through (see `run.sh`).

## GPT → Claude (cross-vendor)

The trial is vendor-neutral. Use **Codex CLI** (a GPT model) as model A and **Claude Code**
as model B:

```bash
# model A: GPT (e.g. gpt-5.x or gpt-6.x) in Codex, non-interactively in the project
codex exec --full-auto -C /tmp/trial/proj "$(sed 's#{PROJ}#/tmp/trial/proj#; s#{AGENT_ID}#gpt (Codex)#' \
    eval/handoff_trials/export_feature/prompt_A.md)"
cp -a /tmp/trial/proj /tmp/trial/proj_b0
# model B: Claude in Claude Code, with and without Continuum
claude -p "$(sed 's#{PROJ}#/tmp/trial/proj#; s#{AGENT_ID}#claude (Claude Code)#' \
    eval/handoff_trials/export_feature/prompt_B_continuum.md)" --output-format json > b_continuum.json
claude -p "$(sed 's#{PROJ_B0}#/tmp/trial/proj_b0#' \
    eval/handoff_trials/export_feature/prompt_B_baseline.md)" --output-format json > b_baseline.json
```

To use MCP tools rather than the CLI, connect `continuum mcp` to each agent as shown in
`integrations/README.md`. For the baseline B run, make sure Continuum is **not** connected.

In a Claude Code cloud environment, the GPT side needs `OPENAI_API_KEY` as an environment
secret and `api.openai.com` in the environment's allowed domains.

---

_Continuum is proprietary and confidential. © 2026 the Continuum copyright holder. All rights reserved. See [LICENSE](../../LICENSE)._

---
name: continuum
description: Delegate a pre-registered multi-step task (uploads, database updates, notifications, file pipelines) to Continuum so progress survives crashes and model switches and side effects never repeat. Use when the user asks to run, resume, check or inspect a Continuum task or run.
---

# Using Continuum from a coding agent

Continuum runs **pre-registered** tasks. It records progress only when a verifier
proves a milestone, and it ledgers every external effect so retries never duplicate it.

## Workflow

1. `list_tasks` shows what can run. Only these task ids are accepted.
2. `run_task(task_id)` runs until the task completes or pauses. Report the returned `status`.
3. Follow progress with `get_status(run_id)`. To explain *what happened*, use
   `inspect_run(run_id)`: its timeline shows model decisions, effects, reconciliation and
   milestones.
4. If the status is `awaiting_reconciliation` or `paused_for_approval`:
   - call `pending_decisions(run_id)` and show the user each decision and its exact command;
   - **do not decide for the user.** Approving, confirming or retrying an uncertain side
     effect is a human decision, so it is not available as a tool;
   - after the user has run the command, call `resume_run(run_id)`.
5. To continue on a different model (for example after rate limits), call
   `resume_run(run_id, model_alias="B")`. Verified work and confirmed effects carry over.
   Nothing is redone.

## Rules

- Never claim a milestone is done unless `get_status` shows it `verified`.
- An `unknown` effect is not a failure. Do not suggest re-running the task to "retry" it.
- `cancel_run` stops *new* effects only. It cannot undo calls already sent.

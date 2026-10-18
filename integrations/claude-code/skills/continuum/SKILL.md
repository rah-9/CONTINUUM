---
name: continuum
description: Keep coding work continuous across model switches and new chats (e.g. Opus 4.8 to 5.5) with Continuum's coding-session journal and verified checks; also delegate pre-registered multi-step tasks whose side effects must never repeat. Use at the start of every coding session in a repo that uses Continuum, after switching model, and when the user asks to run, resume or inspect Continuum work.
---

# Using Continuum from a coding agent

## Coding continuity (every session in a repo)

- **First call:** `coding_resume(repo_path, agent=<your exact model id>)`. If no session exists,
  call `coding_start(repo_path, goal, agent, tasks=[{"title", "checks"}])`.
- **Read the brief.** "What the user asked" is the user's own words from earlier chats and apps:
  the requirements, newest wins. `verified_facts` were observed by Continuum; `agent_notes` and the
  last agent report are claims from earlier models. The brief may already be in your context
  (shown by a hook at session start); still call `coding_resume` once. Continue from `next_steps` and open tasks. Never redo verified tasks unless
  `needs_reverification` is true.
- **Don't re-read the repository.** Start from "Code touched in this session" (the code map)
  and the next step. Use `coding_brief(detail="full")` only for check output or commands, and
  `coding_recall(query)` to find anything older than the brief shows.
- **While working:**
  - record decisions, context and blockers with `coding_note`
  - call `coding_checkpoint(summary)` after meaningful changes
- **To finish a task:** run its checks with `coding_run_check`, then call
  `coding_complete_task`. A refusal means the checks failed or the code changed since they
  passed.
- **Before stopping:** add a `next_step` note for the next model.
- **Checks belong to humans.** Only a human registers checks. Never change them, and never edit
  tests just to pass them.

## Delegating pre-registered tasks

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

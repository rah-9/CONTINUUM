<!-- Continuum coding-continuity rules. Paste into your repo's AGENTS.md (Codex, Cursor,
     most agents) and/or CLAUDE.md (Claude Code). Works for any kind of coding project. -->

## Continuum: continuity across models and chats

This repository uses Continuum (MCP server `continuum`), so any model or chat can pick up this
work exactly where the last one stopped. Follow these rules in every session:

1. **Start by resuming.** At the start of every chat, and immediately after switching model,
   call `coding_resume(repo_path=<absolute repo path>, agent=<your exact model id>)`.
   - If it reports no active session, call `coding_start` with the goal and the planned tasks,
     each naming the checks that prove it (`checks: ["unit"]`).
   - Read the returned brief before touching code.
   - **Do not re-read the repository.** Start from the files under "Code touched in this session"
     (the code map) and the next step; open other files only when the work needs them.
   - The brief is compact Markdown. Call `coding_brief(detail="full")` only if you need check
     output tails or registered commands.
2. **Trust facts, verify claims.**
   - The brief's `verified_facts` were observed by Continuum: code fingerprint, check results.
   - `agent_notes` are what earlier models said. Confirm critical claims against the code.
3. **Don't redo verified work.**
   - A task marked verified is done unless `needs_reverification` is true; re-run its checks if
     it is.
   - Continue from the most recent `next_steps` and the open tasks.
4. **Leave a trail as you work.**
   - `coding_note(kind="decision")` for choices and why.
   - `coding_note(kind="context")` for things the next model must know.
   - `coding_note(kind="blocker")` for anything blocking.
   - `coding_checkpoint(summary)` after each meaningful change.
5. **Done means proven.**
   - Before claiming a task is done, run its checks with `coding_run_check`, then call
     `coding_complete_task`. If it is refused, the code changed since the checks ran, or a check
     failed. Fix it and re-run; do not argue with the refusal.
   - A task without checks can be completed, but it is recorded as unverified. Prefer naming a
     check.
6. **Before you stop**, record a `next_step` note saying exactly what the next model should do
   first.
7. **Never change how checks are defined.** Only a human registers checks
   (`continuum coding checks add`). Do not edit tests just to make a check pass.
8. **Never record secrets** in notes. Continuum redacts known secrets, but do not rely on it.

**Without MCP** (for example a shell-only agent), use the same protocol through the CLI. The
`--agent` value is your exact model id.

```bash
continuum coding resume --repo . --agent <model>          # or: continuum coding start . --goal "…" --agent <model>
continuum coding note SESSION next_step "…" --agent <model>   # decision|context|blocker|next_step
continuum coding checkpoint SESSION --summary "…" --agent <model>
continuum coding check SESSION <check> --agent <model>       # exit 1 if it fails
continuum coding task done SESSION TASK --agent <model>     # exit 1 if refused
```

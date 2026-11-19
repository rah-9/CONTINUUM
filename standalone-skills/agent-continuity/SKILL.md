---
name: agent-continuity
description: Keep coding work continuous when the user switches model, chat or app (Claude to GPT, Claude Code to Codex or Cursor, a new chat after a limit or crash). Saves the user's requirements in their own words, decisions, checks that are re-validated against the current code, and a ledger that stops a lost git push, deploy or publish from being repeated. Use at the start of every coding session in a project with a .continuity folder, when the user says "continue", "resume", "where were we", "hand off" or is switching model or app, and before any external action (push, deploy, publish, release, migration, sending email or payments).
---

# Agent continuity

A file-based work journal that lives in the project (`.continuity/`, kept out of git), so
the next model, chat or app continues from facts instead of guesses. One Python script,
standard library only (Python 3.9+), no server, no account, no network.

Run it as `python3 <skill-dir>/scripts/continuity.py <command>`, where `<skill-dir>` is the
folder containing this file (on Windows use `python`). Always pass `--agent <your exact
model id>` or set `CONTINUITY_AGENT`. Exit code 3 means a safety rule refused the action:
never work around it.

## 1. At the start of a session

1. Run `resume`. Read the whole brief before touching code.
   - **What the user asked** is the user's own words. When two conflict, the higher `#`
     wins.
   - **Checks** are re-evaluated right now against the files: only "PASSED on the current
     code" is proven. "passed earlier, code changed since" must be re-run.
   - **Notes and checkpoints** are claims by earlier agents. Confirm them before you rely
     on them.
   - Start from **Next step** and **Where the code is**. Don't re-explore the whole
     repository.
2. If the brief says there is no session, run `start --goal "<the user's goal>"`, then
   record each requirement the user gave, in their exact words:
   `note requirement "<their words>"`.
3. If the brief shows **UNKNOWN EXTERNAL EFFECTS**, stop and read section 4 first.

## 2. While working

- The user gives a new requirement or changes one: record it word for word:
  `note requirement "<their words>"` (add `--replaces <#id>` when it replaces an older one).
- You make a decision: `note decision "<what>" --reason "<why>"`.
- Something the next model must know that isn't in the code: `note context "..."`.
- You are blocked: `note blocker "..."`.
- After a meaningful change: `checkpoint "<what changed, what state it is in>"`.
- Anything older than the brief shows: `search <words>`; one item in full: `show <id>`.

## 3. Checks and tasks: progress needs evidence

- Checks are commands such as tests, a build or a linter. **Only the user decides what a
  check is.** Register one only when the user asks or approves:
  `check add <name> -- "<command>"` (optional `--paths src tests` to scope it).
  Never change, weaken or remove a check, and never edit tests just to make them pass.
- `check run [name ...]` runs checks and binds each result to a fingerprint of the file
  contents. Any later edit makes the pass stale; a commit alone does not.
- `task add "<title>" --checks a,b`, then after the work `task done <id>`. This is refused
  unless every check passes on the current code. If it's refused, fix the code, never the
  check.
- Never say something is done or working unless the brief shows it verified.

## 4. External actions: never do one twice

An external action is anything that leaves the machine or can't be undone locally: `git
push`, deploy, publish or release, database migration on a shared database, opening or
merging a pull request, sending email or messages, payments, calls to write APIs.

1. Before it: `effect begin "<exact action>" --key <business-id> --verify "<read-only
   command that succeeds only if it happened>" [--expect "<text in its output>"]`.
   Example: `effect begin "git push origin main" --key push-main-<sha> --verify "git
   ls-remote origin refs/heads/main" --expect "<sha>"`.
   If it's refused, do not run the action.
2. Run the action.
3. Right after: `effect end <id> --outcome done|failed|unknown --result "<what you saw>"`.
   - `done`: it clearly succeeded.
   - `failed`: the error **proves nothing happened** (e.g. rejected for bad credentials
     before sending).
   - `unknown`: anything else, such as a timeout, crash, lost connection, interrupted
     command or unclear output. **When in doubt, it's unknown, not failed.**

While any effect is unknown, every new external action is refused (`guard` exits 3).
To clear it:
- `effect verify <id>` runs the read-only check. Only a positive result counts. "Not
  found" is **not** proof it didn't happen, because remote systems lag.
- Otherwise show the user the effect and ask them. Only when the user has told you the
  answer: `effect resolve <id> --done|--not-done --user-said "<their words>"`.
- Never retry an unknown action to "see if it works". Never resolve on the user's behalf.
- `--user-override "<the user's words>"` on `effect begin` is only for when the user
  explicitly tells you to proceed anyway.

Run `guard` before any external action you didn't wrap in `effect begin`.

## 5. Before stopping, or when the user switches model, chat or app

Run:

```
handoff --summary "<what changed and its state>" --next "<exactly what to do first>"
```

This also captures the user's messages from local Claude Code and Codex chat transcripts
(`capture`). For other apps, make sure each requirement was recorded with `note
requirement`. Then tell the user in two lines what you recorded, that the next model
continues by running `resume`, and whether any external effect is still unknown.

## Rules

- The journal is the source of truth, not chat history. Don't copy unverified claims into
  notes as facts.
- Never edit `.continuity/journal.jsonl` by hand. Use `retract <id>` to withdraw a
  note.
- Don't put secrets in notes. Known key and password formats are masked automatically, but
  don't rely on that.

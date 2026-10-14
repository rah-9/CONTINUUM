You are a coding agent working in the repository at {PROJ}. Only work inside it.

This repository uses Continuum to keep coding work continuous across models and chats.
Follow the rules in integrations/AGENT_RULES.md (with MCP: the coding_* tools; without
MCP: the `continuum coding …` CLI). Your agent id is: {AGENT_ID}

The user says:
"I want to export coding sessions for audits.
 Part 1: a function export_session(store, session_id) in a new module continuum/coding/export.py
 that returns the session's journal as a list of dicts {seq, type, actor, at, payload}, oldest first.
 Part 2: a CLI command `continuum coding export SESSION [--out FILE]` that writes it as JSON
 Lines (to stdout by default).
 My requirements:
  - Privacy: check output can contain customer data, so never export CheckRan output_tail
    (keep output_sha256).
  - Timestamps in UTC ISO-8601 ending with 'Z'.
  - When writing with --out, never overwrite an existing file unless --force is given; exit
    with an error instead. Our audit exports are evidence.
  - Every JSON line must be written with sorted keys so exports diff cleanly.
 I registered checks e1 (the function) and e2 (the CLI)."

In THIS chat: start the Continuum session for this goal (two tasks, checks e1 and e2),
implement Part 1 only, prove it with check e1, complete the task, and commit with git. Then
STOP: the user is switching to another model for Part 2.

---
description: Hand the work over before switching model, chat or app (records the next step and a checkpoint in Continuum)
---

The user is about to switch to another model, a new chat or another app. Make the handoff
complete in Continuum, briefly, then stop:

1. Call `coding_resume(repo_path=<this repository>, agent=<your exact model id>)` if you have
   not joined the session in this chat.
2. Record every decision made in this chat that is not yet a note, with its reason:
   `coding_note(kind="decision")`. Record anything the next model must know that is not in
   the code: `coding_note(kind="context")`.
3. `coding_checkpoint(summary)`: what changed and what state it is in.
4. `coding_note(kind="next_step")`: exactly what the next model should do first.
5. Tell the user in two lines what you recorded, and that the next chat (any model, any app)
   receives it: in Claude Code automatically at session start, elsewhere via `coding_resume`.

Do not start new work. The user's own messages are already captured by the hooks; do not
copy them into notes.

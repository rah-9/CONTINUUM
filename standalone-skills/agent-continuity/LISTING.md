# Marketplace listing draft (not shipped in the skill)

**Name:** Agent Continuity

**Tagline:** Switch model, chat or app mid-task without losing the user's requirements and
without ever pushing or deploying twice.

**Description**

When a chat hits its limit, crashes, or you move from Claude to GPT or from Claude Code to
Codex or Cursor, the new model starts blind. It re-reads the repo, misses what you said
twenty messages ago, believes "tests pass" from a chat it never saw, and may redo a push or
deploy that already happened.

Agent Continuity keeps the work in a small journal inside your project:

- **Your words, not a summary.** Requirements are kept verbatim, newest wins. Messages are
  captured automatically from Claude Code and Codex chats on your machine.
- **Proof, not claims.** Checks such as your tests are bound to the exact file contents.
  Edit a file and the old pass is marked stale. Tasks can't be marked done until their
  checks pass on the current code.
- **No double side effects.** Pushes, deploys, publishes and emails go through a ledger. If
  one's result was lost, every new external action is blocked until it's verified or you
  decide.
- **A compact brief** (about 3k tokens) for the next model: goal, requirements, next step,
  tasks, checks, decisions, where the code is.

One Python file, standard library only (3.9+). No server, no account, no network, no API
keys. The journal stays on your machine, out of git, with secrets masked.

**Works with:** any agent that can read `SKILL.md` and run a shell command (Claude Code,
Codex, Cursor, Gemini CLI, Copilot agents …). Chat capture is automatic for Claude Code and
Codex; with other apps the agent records requirements itself.

**Honest limits**
- It's a skill, so the agent is *asked* to follow the rules. The script refuses unsafe
  steps (exit code 3), but an agent that ignores the skill entirely isn't stopped.
- Automatic recording of every tool call needs hooks; this skill doesn't install any.
- One journal per project folder. It isn't built for many agents writing at once on
  Windows (no file lock there).

**Price:** to be decided by the author.

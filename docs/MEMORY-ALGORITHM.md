# Memory algorithm: how coding work survives a change of model, chat, app or machine

This is the complete logic Continuum uses to carry coding work from one agent to the next.
It covers what is remembered, how each piece is collected, how the next model's brief is
chosen, and how a repository is recognised. It also lists every scenario this has been
tested against, and the ones it cannot handle. The rules here are normative for
`src/continuum/coding/` (docs/02 §10, D-035, D-036).

**No LLM is involved anywhere.** Every step below is deterministic code: git, the database,
string matching. The only model cost of a handoff is the next model reading the brief,
about 1–3k tokens.

## 1. Principles

1. **Three kinds of memory, never mixed:**

   | Kind | Examples | Trust |
   |---|---|---|
   | **Observed facts** | the code's fingerprint, the code map, commits, check results | proof |
   | **The user's words** | every message the user typed, in any chat, in any app | requirements; the newest wins |
   | **Agent claims** | notes, checkpoint summaries, the last agent report | useful, but must be confirmed |

   The brief labels each. A model's claim is never shown as a fact or as the user's words.
2. **Memory must not depend on the agent's discipline.** The user's words and the code
   map are collected by the app's hooks and by git, even if the agent never calls a
   Continuum tool, forgets to write notes, or is cut off mid-turn.
3. **Bounded context, unbounded memory.** The brief has fixed caps however long the work
   runs. The append-only journal keeps everything, and `coding_recall` finds it.
4. **Never guess.** A repository is joined only by a matching path or by a matching
   remote **and** branch. A fingerprint is never computed from partial data. An unknown
   value is shown as unknown.
5. **Format-independent.** Chat transcripts are read by their structure, not by one app's
   schema, so a new app version degrades to fewer captured messages, never to a crash.
6. **A hook never hurts the agent.** It always exits 0, waits at most a few seconds, and
   logs failures to `$CONTINUUM_HOME/hooks.log`.

## 2. What is stored

One append-only journal per session (`coding_events`, gapless `seq`, immutable; docs/03):

| Event | Written by | Holds |
|---|---|---|
| `SessionStarted` | an agent, or capture | goal, observed `RepoState` (fingerprint, HEAD, branch, changes) |
| `AgentJoined` | each model/chat that resumes | model id, observed state, and the new root if it moved |
| `TaskAdded`, `TaskStatusChanged` | agent | tasks; `verified` only with valid check evidence |
| `NoteRecorded` | agent | decision, context, blocker, next_step (claims) |
| `CheckpointCaptured` | agent | summary (claim) + observed state (fact) |
| `CheckRan` | Continuum, running a human-registered check | exit, redacted output tail, fingerprint before the run |
| `ChatCaptured` | the app's hooks, transcript import, CLI | `role` user/agent, text, source app, chat id, dedupe `key` |
| `SessionClosed` | agent or human | reason |

Sessions carry `repo_root` (the checkout in use) and `repo_key` (the normalized remote).

## 3. Algorithms

### 3.1 Recognising the repository (`repo.canonical_root`, `service._locate`)

```
locate(path):
  given = resolve(path)                          # containment: never / or ~ itself
  root  = git top level of given, if acceptable  # repo/src -> repo
  if an active session has repo_root in {root, given}:   return it          (same checkout)
  key = normalize(remote.origin.url)             # https://u:t@GitHub.com/Org/R.git
                                                 #   -> github.com/org/r  (no credentials)
  for active sessions with repo_key == key, newest first:
      if their last observed branch == current branch:  return it, moved=True
  if any matched by key only: refuse, naming them   # another branch: never guessed
  not found -> coding_start (or capture starts one)
```

When a session moves, `AgentJoined.repo_root` records the new checkout and the session's
`repo_root` follows it in the same transaction. Hooks follow too (`service.follow`), so the
first thing to run in a new checkout (often the session-start hook) already observes it. Checks are looked up by checkout first,
then by `repo_key`, so a check a human registered on their laptop also runs in a cloud
container's clone. An explicit `coding_resume(session_id, repo_path)` may move a session
only to a checkout with the same remote.

### 3.2 Fingerprint (unchanged, D-035)

The git tree of the working copy's content, built in a private index: tracked files as on
disk, plus untracked non-ignored files from one `git status`. Five git processes. Commits
of identical content do not change it; any edit does. Outside git: hash of every file.
Verification: a task is verified only if each check it names passed on the current
fingerprint with its current definition.

### 3.3 Capturing the conversation (`hooks`, `transcript`, `service.capture`)

Inputs, all optional, from any app:

| Field (any of these names) | Meaning |
|---|---|
| `prompt`, `input-messages`, `input_messages` | the user's words this turn |
| `last-assistant-message`, `last_assistant_message` | the agent's report this turn |
| `transcript_path` | the chat: its newest 2 MiB at a turn's end; all of it (newest 64 MiB) at compaction and session end, which catches up any missed turn |
| `cwd` | which repository (else the process directory) |
| `session_id`, `thread-id`, `conversation_id` | which chat |
| `hook_event_name == SessionStart` | print the brief instead (see 3.6) |

Transcript extraction (`transcript.extract`), for every record of a JSON Lines file or one
JSON document:

```
walk(node):                                       # depth <= 8
  skip nodes marked isMeta / isSidechain / isCompactSummary
  if node.role in {user, human}            and has content|parts: emit user text
  if node.role in {assistant, model, ...}  and has content|parts: emit agent text
  if node.type in {user_message, agent_message} and node.message is text: emit
  else walk every child object/list
text of content = str, or the str / text|input_text|output_text parts of a list
                  (tool calls, tool results, reasoning, images are not conversation)
user text      -> remove harness-injected blocks (<system-reminder>, <environment_context>,
                  <user_instructions>, command wrappers, ...) and harness-only messages
drop empty messages and immediate duplicates (an item and its event)
```

Storing (`service.capture`), in one transaction under the session row lock:

```
for each message (only the newest agent message is kept: the turn's final report):
  text = redact(text)        # known secrets, URL credentials, well-known token formats
  key  = sha256(source, chat, role, text)
  store ChatCaptured(text[:16000], chars_omitted) unless key already in the session
no active session, a user message present, inside a git repository
                                    -> start one; goal = the first message
                                    (a plugin's hooks run in every folder; elsewhere a
                                     session starts only with an explicit coding_start)
```

So re-importing a transcript after every turn is idempotent, and concurrent hooks cannot
duplicate a message.

### 3.4 Assembling the brief (`brief.SessionView.to_dict`, `render_markdown`)

From the journal plus a fresh observation. Every cap is fixed or grows slowly with the
chat up to a cap, and every omission is counted and shown. The user's side of the
conversation is consolidated (`coding/memory.py`, D-037); the measured quality is in
[MEMORY-QUALITY.md](MEMORY-QUALITY.md):

```
consolidate(turns):                                     # every captured message, in order
  number user messages #1..#N
  latest  = #N whole (2,000 chars); flag ends_chat ("stop here", "next model will ...")
            + the agent's question it answers, if #N is a short reply
  first   = #1 (700 chars), as prose
  for each earlier message m:
      if m is a short reply and the agent's message before it asked a question:
          candidate(answer, strength 3, 'asked "Q?" -> "yes"')
      for each sentence s of prose(m):        # code blocks, traces, logs, diffs removed;
                                              # a pasted block runs until a blank line
          revision cue ("actually", "instead of", ...)  -> strength 3
          strong cue ("must", "never", "only", ...)     -> strength 2
          weak cue / preference / spec line / value statement -> strength 1
          question, small talk, short reply             -> dropped
          anything else                                 -> statement, strength 0
      at most 6 candidates per message
  identical sentences: keep the newest occurrence
  fill the ledger by (strength, newer) within 4,400 chars + 50 per message beyond 40
      (cap 10,000); show it in conversation order with message numbers
  agent decisions = choice/caveat sentences from the agent's reports, newest 8 (+1 per
      40 reports, cap 14), as claims
```

| Section | Selection | Bound |
|---|---|---|
| Attention | rules in 3.5 | a few lines |
| What the user asked | consolidation above: first request, ledger, most recent message (with the question it answers) | latest 2,000 chars; ledger 4,400–10,000 chars |
| Reported choices and caveats | decision sentences from captured agent reports (claims) | 8–14 items, 260 chars each |
| Verified facts | current code, commits since the last checkpoint, latest check per name | 50 files, 10 commits |
| Code map | files changed since the session's base commit (git) or modified since it started (mtime, outside git), +/- lines, enclosing context, names defined | 40 files (15 in Markdown), 8 names each |
| Tasks | all, with verified / needs re-verification | — |
| Agent notes | newest 8 decisions, 6 context, 8 blockers, 3 next steps, last 5 checkpoints | 1,000 chars (600 for context) |
| Last agent report | newest captured agent message | 1,500 chars |

Agent-written and captured text is flattened to single lines in Markdown, so it cannot
forge the brief's own headings. MCP returns the Markdown plus task ids (compact) unless
`detail="full"`.

### 3.5 Attention rules (computed, not written by a model)

- No chat captured: requirements stated only in chat may be missing; ask the user.
- The user's most recent message ended the previous chat ("stop here", "the next model will
  do it"): the stop was addressed to the previous agent; the remaining work is the next
  one's. Otherwise, if that message is newer than the newest `next_step`, it says what to
  do now.
- Nothing was recorded for more than 3 days: confirm the latest request is still current.
- The code map is non-empty and the code differs from the last checkpoint (or there is
  none): read the mapped files first.
- Verified tasks whose code or check definition changed: re-verify them.
- A check failing on the current code.
- The session moved to another checkout; the branch differs from where it started.

### 3.6 Restoring memory at the start of a chat

- **Apps with a session-start hook** (Claude Code): the hook prints the brief, which the
  app puts in the new chat's context before the model's first turn. No tool call needed.
- **Every app:** the rules in `AGENTS.md` (also imported by `CLAUDE.md`) tell the model to
  call `coding_resume` first. That records which model joined and returns the same brief.
- **Searching older memory:** `coding_recall(query)` matches query words against every
  goal, task, note, checkpoint, captured message and check output in the session (or all
  sessions of the repository). It ranks by distinct words matched, then newest first, and
  returns bounded snippets.

## 4. Cost

| Operation | Work |
|---|---|
| fingerprint | 5 git processes (~165 ms on a 20k-file repo) |
| locate | 1 indexed query; +2 git calls only when no session is at the path |
| capture hook | at a turn's end the newest 2 MiB of the transcript; 1 transaction |
| brief | 1 journal read + fingerprint + code map (3 git calls) |
| recall | 1 journal read per session searched; no index to maintain |
| tokens | compact brief ~0.7k–1.8k for chats up to 80 messages, ~3k at 160 (measured); full JSON < 10k |

## 5. Scenarios

Each row is an automated test against PostgreSQL and real git. Hook payloads and
transcripts are built with each app's structure.

| # | Scenario | What carries over | Test |
|---|---|---|---|
| S1 | Claude Code → Codex; A never used Continuum; the chat ended abruptly; B starts in a subdirectory; then back to Claude Code | user's requirements verbatim and in order, last report, code map, attention; one session | `test_scenarios.py::test_s1_…` |
| S2 | laptop → cloud container (other path, other remote URL spelling, same repo and branch) | session, notes, the human's checks; verification works there | `test_s2_…` |
| S2b | same repository on another branch | nothing joined by guessing; candidates named; explicit session id allowed | `test_s2b_…` |
| S3 | hooks fire repeatedly and concurrently; the same text via hook and import | no duplicates in journal or brief | `test_s3_…` |
| S4 | database down, malformed payloads, stdin never closed | the app is unaffected: exit 0, no output, logged | `test_s4_…`, `test_s4b_…` |
| S5 | very long session (60 requests, 90 notes) | brief < 4k tokens, first and newest requests; omitted details found by recall | `test_s5_…` |
| S6 | project without git | code map by modification time | `test_s6_…` |
| S7 | a secret pasted into chat | never stored | `test_s7_…`, `test_redaction.py` |
| S8 | "ok"/"continue" turns; conflicting requests | short replies hidden (newest kept), newest wins | `test_s8_…` |
| S9 | session closed, new work begins | a new session; agent text alone never starts one | `test_s9_…` |
| S10 | one-command setup in a repo with existing rules and hooks | merged, idempotent, nothing of the user's lost | `test_s10_…` |
| S11 | existing v3 database | upgraded in place | `test_s11_…` |
| S12 | hooks installed for every project; the user chats in a non-repository folder | nothing journaled unless a session is started explicitly | `test_s12_…` |
| S13 | the user says "yes" or "continue" several times in one chat | every one kept; transcript re-imports add nothing | `test_s13_…` |
| — | long chats of 10–160 messages with requirements, changes of mind, answers, agent decisions and pasted noise anywhere | all of them in the brief, no noise, ≤ ~3k tokens | `continuum_lab.memory_bench`, `test_nf6_long_chat_memory_quality`, `tests/unit/test_memory.py` (with a property test) |
| — | model or version switch in one app; new chat; vendor switch | everything above | `test_coding.py` C1–C11, `test_mcp_stdio.py` |
| — | commit, rebase, failing/timed-out checks, forged text, races | facts stay correct | `test_coding_hardening.py`, `test_brief_quality.py` |
| — | transcript formats: Claude Code, Codex, Gemini, generic chat export, an unknown future wrapper, garbage lines | user text only, no injected context | `tests/unit/test_transcript.py` |

Real models: see [HANDOFF-TRIALS.md](HANDOFF-TRIALS.md), Trials 4 and 5, and the summary
in [MEMORY-QUALITY.md](MEMORY-QUALITY.md).

## 6. Support by app

| App | Capture of the conversation | Brief at chat start | Rules |
|---|---|---|---|
| Claude Code | plugin or `continuum coding setup` hooks: every prompt, every turn's report, the transcript | yes (SessionStart hook) | CLAUDE.md → AGENTS.md |
| Codex CLI | `notify` hook: every turn's prompts and report | via the rule: `coding_resume` | AGENTS.md |
| Gemini CLI and other hook-capable apps | `continuum hook <app>` with the app's JSON payload (fields in 3.3) | if the app prints hook output into context | AGENTS.md (or the app's rules file) |
| Cursor, VS Code, Windsurf, Cline, Zed, JetBrains | `continuum coding import <chat file>` or `continuum coding capture` | via the rule: `coding_resume` | AGENTS.md / the app's rules |
| shell-only agents | `continuum coding capture` | `continuum coding resume` | AGENTS.md |

## 7. Limits

- **Apps without hooks** capture nothing automatically. The brief then has the code map,
  facts and agent notes, and its Attention section says that requirements stated only in
  chat may be missing.
- **Transcript formats** can change. The parser is structural and tested on several
  formats, but a radically different format yields fewer messages. The per-turn fields
  (`prompt`, `last-assistant-message`) do not depend on it.
- **Redaction** removes known secret values, URL credentials and well-known token formats.
  A secret in an unknown format pasted into chat can still be stored. Do not paste secrets.
- **Tamper evidence, not prevention:** anything with shell access can run the CLI (capture,
  check registration). Every entry records its source (D-029).
- **One active session per checkout.** Two unrelated tasks in one checkout share a session
  until it is closed; recall and the newest-wins rule keep this workable.

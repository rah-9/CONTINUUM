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
   | **Working memory** | a model's structured account of the whole session (D-048) | a claim, with the event it covers up to; checked with recall |
   | **Session digest** | quoted statements of the whole session, chosen without a model (D-049) | quotes with their [#N]; the user's lines are requirements, the agent's lines claims |

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
6. **A hook never hurts the agent.** It always exits 0 and never waits for the database:
   it redacts the payload, writes it to a local spool and exits (about 100 ms). A background
   drainer writes the journal. Failures go to `$CONTINUUM_HOME/hooks.log`.
7. **Everything the agent did, verbatim** (D-042). Every tool call with its full (redacted)
   result, what each call changed in the files (observed with snapshots), subagents and
   compaction. Derived structure (the ledger, the work summary) only points back to it.

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
| `ChatCaptured` | the app's hooks, transcript import, CLI | `role` user/agent (and `action` in sessions from 1.0.0rc1), text, source app, chat id, dedupe `key` |
| `ToolCallStarted` | the app's hooks or transcript | tool, kind (builtin/shell/mcp/skill/subagent), MCP server, skill, subagent id, effect (read/write/external/delegate), redacted input, content snapshot before a call that can write |
| `ToolCallFinished` | the app's hooks or transcript | status (ok/error/denied/interrupted), exit code, duration, head and tail of the redacted output, full output blob, output signals, snapshot after |
| `ToolCallUnresolved` | Continuum's reconciliation | a call with no result after its turn ended or its app crashed: outcome unknown |
| `ToolCallResolved` | a person (CLI) | what happened to an unknown call: applied / not_applied / accepted_unknown |
| `FileImpactObserved` | Continuum (git snapshots) | files changed by a call (or between calls), status, +/- lines, diff blob |
| `SubagentReported` | the app's `SubagentStop` | a subagent's final report (a claim) |
| `CompactionObserved` | the app's compaction hooks or transcript | trigger, the app's summary (a claim) |
| `SessionClosed` | agent or human | reason |

Full outputs, diffs and long inputs are blobs (`coding_blobs`: session, sha256 digest,
content up to 1,000,000 characters with head and tail kept beyond that), immutable, under the
session's row-level security and erased with it.

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
  agent content parts of type tool_use, Gemini functionCall parts, and Codex
      function_call / local_shell_call / custom_tool_call items: emit an action
  else walk every child object/list
text of content = str, or the str / text|input_text|output_text parts of a list
                  (tool results, reasoning, images are not conversation)
action = one line: "edit: <path(s)>" (edit/write tools, apply_patch file headers),
         "read: <path>", "run: <first line of the command> … (+n lines)",
         "search: <pattern>", "fetch: <url>", "delegate: <description>", "tool: <name>";
         bookkeeping tools (todo lists, plan mode, questions, Continuum's own) are skipped
failed = the call's result is an error (Claude Code is_error) or a non-zero exit code
         (Codex output), matched by call id across the transcript
user text      -> remove harness-injected blocks (<system-reminder>, <environment_context>,
                  <user_instructions>, command wrappers, ...) and harness-only messages
drop empty messages and immediate duplicates (an item and its event); actions repeat
                  for real (the tests run twice) and are never collapsed
```

Records: when a transcript identifies its messages (Claude Code `uuid`, Codex `timestamp`),
records without an id are copies (request and cache blobs) and are skipped. Each message
keeps its transcript id and timestamp.

Storing (`service.capture`), in one transaction under the session row lock:

```
from a transcript: every user message, every agent text message and every action (tool
                   output is not kept); from a live turn (prompt / notify): the user's
                   words and the turn's final agent message
text = redact(text)        # known secrets, URL credentials, well-known token formats
key  = sha256(source, chat, role, text)
  + "@" + id hash          if the transcript gave the message an id: exact identity, so a
                           re-read (after an outage, or of a 2 MiB tail) adds each missed
                           message once and nothing twice
  + "#n"                   otherwise: the n-th occurrence of these words in the chat, so a
                           second "yes" is a new message
a transcript copy of a message already captured live is stored as a hidden alias (re-imports
  stay idempotent); the live message takes the copy's timestamp
store ChatCaptured(text[:16000], chars_omitted, sent_at) unless the key exists
the brief orders the chat by transcript timestamps, so recovered messages fall into place
no active session, a user message present, inside a git repository
                                    -> start one; goal = the first message
                                    (a plugin's hooks run in every folder; elsewhere a
                                     session starts only with an explicit coding_start)
```

So re-importing a transcript after every turn is idempotent, and concurrent hooks cannot
duplicate a message.

### 3.3b Capturing what the agent did (`activity`, `snapshot`, `spool`, `recorder`; D-042, D-045)

```
hook call (any app) -> activity.from_hook: one record shape for every app
    Claude Code / Codex  PreToolUse, PostToolUse, PostToolUseFailure  (tool_use_id pairs them)
    Gemini CLI           BeforeTool, AfterTool
    Cursor               preToolUse, postToolUse, postToolUseFailure (tool_output,
                         error_message, failure_type, duration); older Cursor: before/after
                         Shell/MCPExecution, afterFileEdit
    all                  SubagentStop (report; Cursor: summary), SubagentHandback (Claude
                         Code), Pre/PostCompact (summary)
  no call id (Gemini, older Cursor): key = chat + tool + input; a repeat after the earlier
    call finished is a new call (key#2); a finish pairs with the oldest open start
  classify(tool, input) -> kind + effect, by a fixed table, never a model:
    read      Read, Grep, Glob, WebFetch, MCP get_/list_/search_... (unless the rest of the
              name writes: get_or_create), skills, Continuum's tools, harness bookkeeping
    write     Edit, Write, apply_patch, ...; shell commands by default
    external  git push, gh pr create, npm publish, kubectl apply, terraform apply, cloud CLIs
              (except describe/list/get), curl with a body or POST/PUT/PATCH/DELETE, database
              clients, ssh/scp; also inside wrappers (git -C dir, bash -c "...", sudo -E,
              npx, python -m); MCP tools that are not reads; any unknown tool
    delegate  Task/Agent (a subagent; its own calls carry its agent id)
  external call while an external call's outcome is unknown (deny list of the project or
    any parent folder): refuse it in the app's own format (Claude Code / Codex
    hookSpecificOutput, Gemini decision, Cursor permission; unknown app: exit 2), decided
    before any snapshot
  write or shell call: snapshot.take within 5 s = git tree of the working copy's content,
    before and after (a private index, objects in the repository's own store); outside
    git, the same in a private store $CONTINUUM_HOME/snapshots/<dir hash> (at most 20,000
    files of at most 10 MB; never the home directory)
  redact -> spool file (atomic rename) -> start the background drainer -> exit (~100 ms)
drainer (one at a time, an OS file lock; deletes a spooled file only after its transaction
    commits):
  recorder.apply in one transaction under the session lock, idempotent by call key:
    start   -> ToolCallStarted (input redacted; inline up to 16,000 chars, else a blob)
    finish  -> ToolCallFinished (a missing start is recorded first); output redacted, full
               output a blob, head 1,500 + tail 1,500 chars inline, errors head + tail;
               signals: "1 failed" / FAILED / Traceback in the output's last 20,000 chars
            -> ToolCallUnresolved after it, for an external call that ended interrupted,
               timed out or disconnected (it may have taken effect); a plain failure with
               an exit code is a known outcome
            -> FileImpactObserved when the snapshots differ: git diff-tree, diff redacted
               into a blob; credential files (.env*, keys) listed without content
            -> the deny list is refreshed when a result arrives for a blocked call
    turn end-> FileImpactObserved for changes between calls (the newest known snapshot
               against the turn's); every write/external call of this chat without a result
               -> ToolCallUnresolved
    a new chat starts -> other chats' open write/external calls -> ToolCallUnresolved
  unknown outcomes are read in journal order: a later result clears a mark, a person's
    resolution always does
  after the batch (outside the transaction): each unresolved external `git push` is probed
    read-only (git ls-remote); remote branch == pushed commit -> ToolCallResolved applied
    with evidence (reconciler:git, D-047); anything else stays unknown
transcript (turn end, catch-up): transcript.activity reads tool_use/tool_result (Claude Code,
  sidechains with their agent id), function_call/exec_command_end/patch_apply_end (Codex),
  functionCall/functionResponse (Gemini) -> the same records, so the live hooks and the
  transcript add each call once
```

### 3.3c Lossless capture and the chat's real shape (`segments`, `media`, `transcript`; D-046)

```
every message      -> stored whole: inline up to 16,000 chars (head + 4,000-char tail),
                      the whole text as a session blob beyond that
images, documents  -> Claude/MCP/Codex/Gemini/OpenAI parts -> coding_media (bytes, sha256,
  (chat and tools)    type, pixel size from the header; <= 10 MB) -> "[image #abc: png WxH]"
                      in the brief; coding_view_image shows it; never interpreted
pasted content     -> segments.segment(): prose | traceback | diff | json | table | log |
                      shell | code (fence tag or structure; a block runs to the next blank
                      line, JSON and tables end where they parse/stop)
                   -> summary by type: error signature (type: message, innermost frame in
                      the project's own code), files and +/-, keys, rows x columns, levels and
                      first error, commands, defined names
                   -> in place of the paste in the brief; the newest 5 (errors first) in
                      "Pasted by the user" (never a requirement); identifiers split into
                      parts for recall
conversation tree  -> Claude Code: uuid/parentUuid (logicalParentUuid across compaction);
                      active path = newest record back to the root; a user message beside
                      it (same parent) is abandoned with everything under it
                      Codex: thread_rolled_back {num_turns} abandons the last turns
                   -> new messages stored with withdrawn; already captured ones withdrawn by
                      an appended ChatWithdrawn (a live message through its transcript copy)
                   -> not requirements; counted in the brief; still in recall
```

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
  a short reply's question is the agent message before it in conversation order (by
      transcript time), not journal order: another app's transcript may be imported later
  the same words from two capture routes (a hook and an import) within 30 seconds are one
      message; the same words later or in another app are a new one
  identical sentences: keep the newest occurrence
  a change of mind that names what it replaces ("B instead of A", "rather than A", "from A
      to B", "replace A with B", "no longer use A"), in any user message including the most
      recent: every earlier item containing all of A's distinctive terms (a value or
      identifier if there is one) and sharing its context (a word of the later sentence
      outside A, several words of A, or a quoted identifier) is marked replaced; shown
      ~~struck~~ "(replaced by #n)" and ranked last for the budget. Matching A's terms
      without the context is only "#n may replace this; check" (D-044). The first
      request and recent whole messages are checked too. Only the user's own words
      replace: an answer item, which quotes the agent's question, never does.
  fill the ledger by (strength, grounded, newer) within 4,400 chars + 50 per message
      beyond 40 (cap 10,000); grounded = names something the agent's tool inputs or
      changed files touched; a plain statement nearly duplicating one already kept (word
      overlap > 0.8) is skipped; show it in conversation order with message numbers
  chats of 12+ messages: an index of every stretch (new app/chat, an hour's pause, or 10
      messages; at most 12 lines), each named by its 4 most distinctive words (tf-idf of
      the person's prose against the rest of the chat): nothing is out of reach
  agent decisions = choice/caveat sentences from the agent's reports, newest 8 (+1 per
      40 reports, cap 14), as claims
```

| Section | Selection | Bound |
|---|---|---|
| Attention | rules in 3.5 | a few lines |
| What the user asked | consolidation above: first request, ledger, most recent message (with the question it answers) | latest 2,000 chars; ledger 4,400–10,000 chars |
| Reported choices and caveats | decision sentences from captured agent reports (claims) | 8–14 items, 260 chars each |
| What the previous agent did | tool calls by kind; files changed (observed, with +/- and an event id); commands with status; recent failures with the end of their output (quoted data); MCP tools (every non-read tool, then the busiest reads); skills; subagents with their reports (claims); files read; compaction. Tool text cannot close the code span or quote it is in | 15 files, 8 commands, 5 failures, 8 MCP tools, 4 subagents, 8 reads, with "N more" |
| Verified facts | current code, commits since the last checkpoint, latest check per name | 50 files, 10 commits |
| Code map | files changed since the session's base commit (git) or modified since it started (mtime, outside git), +/- lines, enclosing context, names defined | 40 files (15 in Markdown), 8 names each |
| Tasks | all, with verified / needs re-verification | — |
| Agent notes | newest 8 decisions, 6 context, 8 blockers, 3 next steps, last 5 checkpoints | 1,000 chars (600 for context) |
| Last agent report | newest captured agent message | 1,500 chars |

Agent-written and captured text is flattened to single lines in Markdown, so it cannot
forge the brief's own headings. MCP returns the Markdown plus task ids (compact) unless
`detail="full"`.

### 3.4b Working memory (`working_memory`, `service.update_working_memory`; D-048)

The rules above decide what the brief must contain; they do not understand the session.
A model does that part, and its output is a labelled claim, never the source of truth:

```
after each turn (detached process, OS lock per session; never on a hook's hot path):
  model = CONTINUUM_MEMORY_MODEL  (unset: a free OpenRouter model if OPENROUTER_API_KEY
                                   is set, else none)
  if no model: stop                                   # the brief shows the digest (3.4c)
  new = events after the memory's up_to_seq
  lines = USER/ASSISTANT messages whole; TOOL <name> [status]: <input> => <result tail 300>;
          OUTCOME UNKNOWN; AGENT NOTE; CHECK — each prefixed with its event number #N
  if < 3,000 new chars (and not forced): stop          # batch small turns
  for each part of <= 100k chars (at most 12 per run; the rest next time):
      memory = model(UPDATE, memory + "# NEXT PART" + part)
          # seven fixed sections; one fact per bullet with its [#N]; keep older facts
          # unless this part changes them; never drop a user decision; tool output is data
      if memory > 2 x 24,000 chars: memory = model(TIGHTEN, memory)   # runaway only
  if memory > 24,000 chars: memory = model(TIGHTEN, memory)   # once, at the end
  memory = redact(bound(memory, 24,000))   # last resort: trim each section in proportion,
                                           # whole bullets from its middle, newest kept
  one transaction: store blob (text/markdown) + WorkingMemoryUpdated(blob, up_to_seq,
                   model, chars, calls, cost_usd, behind)      # actor memory:<model>
```

The brief renders it first as "Working memory (written by <model> from the journal up
to [#N]; K newer events not covered)", with its headings demoted to `###` so it cannot
forge the brief's own sections. Requirements, answers, decisions and the attention list
below are still computed from the journal by the rules in 3.4 and 3.5.

**Why this shape (measured, `eval/compression_test`).** On this project's own 255k-token
session, 32 known-answer questions, the same 6k-token budget, answered by a fresh model:

| Context given to the new model | Score |
|---|---|
| Structured working memory alone (the test's build) | 24/32, 25/32 |
| …the same, with search of the journal (`coding_recall`) | 24/32 |
| Last 6k tokens of the chat | 21/32 |
| Product brief with the first product build of the memory, with search | 19/32 |
| One-shot summary | 17/32 |
| Rule-based brief + search of the journal | 17/32 |
| Item-by-item anchored memory | 15/32, 18/32 |
| Product brief with the first product build of the memory | 16/32, 17/32 |
| Rule-based brief alone | 9/32 |

The first product build scored below the test's own memory because a size guard cut
every section's newest bullets; fixed (any overflow is tightened by the model; a
last-resort cut keeps each section's first and newest bullets), with the cap raised to
24,000 characters, the size measured; re-scored, the fixed build answered 12/32 alone and
18/32 with search, still below the test's own build (open work: it is tightened along the
way, the test's build was not). An earlier "32/32 with search" was invalid: the
searched journal contained the test's answer key (`eval/compression_test/RESULTS.md`).

The memory makes the brief itself useful; the verbatim journal stays the source of truth
and is searchable. Both are kept.

### 3.4c Session digest, no model (`digest`; D-049)

When no model is configured (the default without an `OPENROUTER_API_KEY`), or before a
model has written its first memory, the brief leads with the session digest. It covers
the whole session, not only the chat: what the user said and pasted, what the agent
reported, and what the tools did (files changed, commits, pull requests, test and build
results, failures and their fixes, calls whose outcome is unknown). It is built from the
journal on every brief, so it is never behind:

```
lines = history_lines(journal)       # tool inputs: paths and short fields first,
                                     # long contents as head + length
turns = split at each USER message
units:
  user     = the user's prose by sentence; a run-on sentence (voice typing) by clause
             (<= 200 chars); the best-scoring one of each message becomes its request,
             unless the message is chatter (< 6 words, nothing specific)
  paste    = each pasted block (segments.py): whole when short, else its summary (an
             error signature with file, line and function; JSON keys; line counts)
  answer   = each "question -> answer" the user gave to a multiple-choice question
  report   = sentences of the agent's last message of the turn
  agent    = other agent prose (code blocks skipped; sentences ending in ":" or "?"
             skipped: they announce work, not results)
  edit     = one line per turn: every file the turn changed, each once
  outcome  = commit subjects; pull requests opened or merged
  result   = the closing summary of a successful command ("214 passed in 41.0s")
  failure  = a failed command with the line naming its error (else its closing
             summary); "(passed at #N)" added when the same command later succeeds
  unknown  = a call whose outcome is unknown
score(u) = prior(kind) x (0.4 + rarity) x (0.5 + specificity) x shortness
  prior: request, answer, paste, unknown 3.0; user 2.5; report 2.2; outcome, edit,
         note 1.6; result 1.4; failure 1.3; agent 1.0
  rarity: mean IDF of the unit's 12 rarest words over the session, scaled to 0..1
  specificity: numbers, versions, #N, paths, identifiers, code spans (capped at 6) / 6
  shortness: 1 when the unit names anything specific, else min(1, length / 60)
always kept (requests, pastes, answers, unknown outcomes), up to 60% of the room:
  above that, requests go first, then pastes, then answers, unknown outcomes last;
  within a tier the least value per character first; never the first request (the
  goal) or the latest; the number left out is printed
then, globally by score / (length + 60), x 0.9 per unit already taken from that turn,
  x 1.6 for the newest fifth of the turns, skipping a unit whose wording overlaps a
  chosen one by > 50% unless it names a number, path or identifier they do not
print: turn by turn in journal order, "- [#N] ...", each heading and line counted
  exactly, so the budget is never exceeded and no line is cut
```

No keyword lists decide importance: only structure (who said it, where, what kind of
tool call) and statistics (specificity, rarity, redundancy). Every line is quoted from
the journal (a commit is shown as "Commit: <subject>", an answer as "User decided: <q>
-> <a>", files as "Changed: ..."), so the digest cannot invent anything.

**Measured.**

| | 16,000 chars | 8,000 chars | 4,000 chars |
|---|---|---|---|
| Benchmark, held-out sessions of 60-300 turns, 13 kinds of facts (`continuum_lab.digest_bench`) | **100%** | **87.6%** | **56.4%** |
| The same, last N characters of the session | 30.6% | 16.1% | 8.6% |
| The same, first version of the digest | 59.7% | 30.8% | 15.7% |

At 8,000 characters for 300 turns, root causes stated in the agent's prose are the
weakest kind (26%); the user's decisions, pastes, measurements, changed files, test
results and unknown outcomes are kept in 98-100%. On the real 255k-token session the
offline check (`eval/compression_test`) gives 19/32 at 24,000 characters (the
model-written memory 20/32). About 0.1-0.2 s for a 1M-character session.

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
- A call that could change something outside the repository has an unknown outcome:
  never repeat it; a person resolves it (`continuum coding resolve`), and until then new
  external calls are denied by the `PreToolUse` hook. Local calls with no result: compare
  the observed file changes with what they were meant to do.
- A recent call failed (error status, or an output that reports failing tests despite exit
  0) and was not re-run successfully, in any app (a broader passing run of the same
  command counts: `pytest` after `pytest tests/x.py`): check whether it is still open.
  "Recent" is among the last 12 calls of its kind, so reads do not push a failing test
  run out. Older failures are not flagged: on a real transcript they were experiments the
  agent had moved past.
- The session moved to another checkout; the branch differs from where it started.

### 3.6 Restoring memory at the start of a chat

- **Apps with a session-start hook** (Claude Code): the hook prints the brief, which the
  app puts in the new chat's context before the model's first turn. No tool call needed.
- **Every app:** the rules in `AGENTS.md` (also imported by `CLAUDE.md`) tell the model to
  call `coding_resume` first. That records which model joined and returns the same brief.
- **Searching older memory:** `coding_recall(query, since, until)` (D-043) searches every
  event (goals, tasks, notes, both sides of the chat, tool inputs and results, file changes,
  compaction) and every stored full output and diff: PostgreSQL full-text search (English
  stemming) merged with exact matching for identifiers. Ranking: distinct words matched,
  then text rank, then newest. `coding_show(session_id, seq=N | blob=...)` opens any `[#N]`
  in the brief, or a stored output or diff, in pages.

## 4. Cost

| Operation | Work |
|---|---|
| fingerprint | 5 git processes (~165 ms on a 20k-file repo) |
| locate | 1 indexed query; +2 git calls only when no session is at the path |
| capture hook | at a turn's end the newest 2 MiB of the transcript; 1 transaction |
| brief | 1 journal read + fingerprint + code map (3 git calls) |
| recall | 1 journal read per session searched; no index to maintain |
| working memory | in the background: one model call per new part of up to ~25k tokens; none when fewer than 3,000 new characters |
| session digest | at brief time, no model: ~0.15 s for a 1M-character session |
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

- **The working memory is a model's account.** It can be wrong or miss something; it is
  labelled as a claim with the event it covers up to, and anything exact is checked with
  `coding_recall`/`coding_show`. Its quality depends on the model chosen.

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

---

_Continuum is proprietary and confidential. © 2026 the Continuum copyright holder. All rights reserved. See [LICENSE](../LICENSE)._

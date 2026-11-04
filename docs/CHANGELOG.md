# Changelog

## 1.0.0rc5 — unreleased

Memory without a model, and free models only by default (D-049).

### Added
- **Session digest (no model):** Continuum's own extractive compression of the whole session: every user request and decision, then the most specific statements of each turn, without near-duplicates, across the whole timeline, each with its `[#N]`. Default when no model is configured; ~0.15 s for a 1M-character session; offline check 20/32, equal to the model-written memory.
- **OpenRouter and any OpenAI-compatible server** as the memory model, with a built-in client (no extra package): `openrouter:<model>` (free `:free` models only unless `CONTINUUM_MEMORY_ALLOW_PAID=1`), `openai-compatible:<model>` with `CONTINUUM_MEMORY_API_BASE`.
- `eval/compression_test`: `build digest`, `build product`, an offline `coverage` score, and model calls through OpenRouter or any OpenAI-compatible server.
- The digest covers the whole session: changed files, commits, pull requests, test and build summaries, failures with their later fix, pastes summarised by type, unknown outcomes (always kept). Benchmark (`python -m continuum_lab.digest_bench`): 100% / 89.2% / 57.8% of facts kept at 16k / 8k / 4k characters on held-out sessions of 60-300 turns.
- Model memory: retries with backoff, fallback models (`CONTINUUM_MEMORY_FALLBACKS`), broken or shrunken answers rejected (the previous memory stays), made-up `[#N]` sources marked.

- `INSTALL.md`: installation written so a coding agent can do it ("install Continuum by following its INSTALL.md"), with one-command options for Claude Code (plugin, or `claude mcp add`) and a check for each step. A test keeps every command in it valid.
- `continuum doctor` reports which session memory the brief will use (and whether its key is set, never the key), and says how to start PostgreSQL when it is unreachable.

- Model memory: each part is sent with its key facts (the part's most specific statements, picked by the digest, with sources), and any user decision or unknown outcome the model dropped is restored from the journal.
- In the brief, the digest covers the work and leaves the user's words to "What the user asked" (they were shown twice); words and sentences in any script are recognised.

### Removed
- Unused code: two error classes, a lab-only runtime method, a manifest loader, an evidence helper, a lock property and two fault-injection helpers.

### Fixed
- A failure was never linked to the later success of a long command (the two were cut at different lengths); they are now matched on the full call.
- A session with six-digit event numbers could make the digest's headings longer than counted, so its newest line was cut; headings are now counted from the session's own numbers.
- `continuum doctor` (and other commands) printed the connection pool's retry errors over and over when the database was down; they now report it once.
- The Claude Code plugin's MCP server had no database default; it now uses `CONTINUUM_DATABASE_URL`, else the local default, like the project configuration.
- Tool inputs were cut at 300 characters in JSON order, so a file write's content hid its path from both memories; paths and short fields now come first.
- The digest could exceed its budget by its headings and cut the newest lines; every line and heading is now counted exactly.

### Changed
- The default memory is the digest, or a free OpenRouter model when `OPENROUTER_API_KEY` is set. Claude Code's command is used only when chosen (`claude-cli:<model>`).
- The model memory grows while it reads a backlog and is tightened once at the end; updates wait for about 3,000 new characters.

## 1.0.0rc4 — unreleased

Working memory that a model keeps (D-048; schema v8). Measured on this project's own 255k-token session in `eval/compression_test` (results in its `RESULTS.md`).

### Added
- **Working memory:** after each turn, a model updates a structured memory of the session in seven sections (goal, user decisions, what was built, problems and fixes, results, research, open items), part by part, in the background. The brief shows it first, labelled as a model's account up to `#N`. Default `claude-cli:haiku` when Claude Code is installed; any LiteLLM provider, a local Ollama model, or an OpenAI-compatible endpoint (`CONTINUUM_MEMORY_MODEL`, `CONTINUUM_MEMORY_API_BASE`); `off` keeps the rule-based brief.
- `continuum coding memory [--update]` shows or updates it.
- `eval/compression_test`: the 32-question test of context-compression methods on a real session.

### Fixed
- Upgrading a database that already had tool-call events failed: an old v4 step re-created the event-type check with a short list (`CheckViolation`). Found by upgrading the live-test database; regression test added.
- The memory's size guard cut each section's end, so the newest facts were lost (late questions 0/4 on the real session). Any overflow is now tightened by the model, and a last-resort cut keeps each section's first and newest bullets. Cap 24,000 characters.
- The compression test's first "32/32 with search" was invalid: the searched journal held the test's own answer key. Withdrawn; `run_eval.py clean-copy` searches a copy cut before the test.

## 1.0.0rc3 — unreleased

Lossless memory with a real grasp of technical content, the chat's real shape, and unknown
outcomes resolved by proof (D-046, D-047; schema v7).

### Added
- **Images and documents** from chats and tool results are stored (`coding_media`) and shown to the next model with `coding_view_image` (MCP, read-only) or saved with `continuum coding media`. The brief labels them `[image #…: image/png 1280x800]`.
- **Technical content understood by structure:** stack traces become error signatures (Python, JavaScript, Java, Go, Rust), and diffs, JSON, tables, logs, shell sessions and code are summarised by type. A new "Pasted by the user" line lists the newest pastes, errors first.
- **Rewinds and edits:** messages on a branch the user abandoned (Claude Code edit or rewind, Codex rollback) are withdrawn from the requirements and counted. They stay searchable.
- **Index of the whole chat:** every stretch of a long chat is named by its most distinctive words.
- **Grounding:** requirements that the agent's work actually touched rank first. Near-duplicate statements no longer use up the budget.
- **Recall** finds identifiers by their parts (`user by id` → `getUserById`) and fuses its rankings.
- **Lost `git push` proven automatically:** a read-only `git ls-remote` that shows the pushed commit records the call as applied and lifts the block. Absence is never inferred.

### Changed
- Messages longer than 16,000 characters keep their head and tail inline and their whole text in a blob. They were cut before.
- MCP: 22 tools (`coding_view_image`).
- The compact brief, recall matches and `coding_show` events have a fixed field order.

### Fixed
- A screenshot returned by a tool was stored as base64 text in the output.
- Edited-and-resent messages were captured as current requirements (seen in a real transcript).
- `continuum coding import` imported a transcript's messages but not its tool calls (found by importing this project's own 33 MB session live: 726 messages and 1,839 tool calls now arrive in 13 s).
- The chat index named stretches by spoken filler ("things", "don", "currently") on a real voice-typed chat; it now skips English stopwords, filler and contractions.

## 1.0.0rc2 — unreleased

Everything the agent did, with results and impact (D-042 to D-044, schema v6). Measured in
`docs/MEMORY-QUALITY.md` and Trial 7 (`docs/HANDOFF-TRIALS.md`).

### Added
- **Every tool call with its result.**
  - Live from the apps' hooks: Claude Code, Codex, Gemini CLI, Cursor.
  - From transcripts, including Claude Code subagents, Codex rollouts and Gemini chats.
  - Covers built-in tools, shell commands, MCP tools (with server), skills (by name) and subagents (their own calls tagged, their reports kept as claims).
  - Full output, secrets redacted, stored as content-addressed blobs under the same row-level security and erasure as events.
- **What each call changed.** Content snapshots (git trees) before and after every call that can write give the exact diff, including a shell command's changes, which app checkpoints miss. Changes between calls are caught at turn end.
- **Unknown outcomes.** A call with no recorded result becomes unresolved at turn end, or when a new chat starts after a crash. While an external one is open, new external calls are denied (`PreToolUse`) until a person runs `continuum coding resolve`. Local work continues.
- **Output signals:** a test run that exits 0 but reports failures (a pipe into `tail`) is flagged.
- **The brief's "What the previous agent did":**
  - files changed (observed)
  - commands with status
  - recent failures with the end of their output (quoted, as data)
  - MCP tools, skills, subagents with reports, compaction
  - an event number on every line
- **Attention flags** unknown outcomes and recent failures.
- **Recall** uses PostgreSQL full-text search over every event and full outputs and diffs, merged with exact matching, with `since`/`until`. `coding_show` (MCP, read-only) and `continuum coding show` open any `[#N]`, or a stored output or diff.
- **Explicit supersession.** A change of mind that names what it replaces marks the earlier requirement `~~#4~~ (replaced by #12)` (user words only).
- **Benchmark measures:** output recall, update correctness, faithfulness, plus transcript and two-app modes. Every measure is 100% at 10–160 messages.
- **Trial 7 (capture fidelity, real Claude Code):** 10 of 10 tool calls with correct results, a subagent's call, the skill, the MCP call and its result, and 3 of 3 changed files, including one written by a script. The handoff scored 4/4 with Continuum for Haiku, Sonnet and Opus, at about half the cost, and 3/4 without: the ticket created through MCP was unknowable.

- **Hooks for Codex, Gemini CLI and Cursor.** `continuum coding setup` now also writes `.codex/hooks.json`, `.gemini/settings.json` and `.cursor/hooks.json`, each in the app's own format (D-045).
- **File impact outside git:** a private snapshot store per directory in `$CONTINUUM_HOME/snapshots/`, erased with the session (D-045).
- **"Possibly replaced"**: a change of mind that matches an earlier requirement only partly marks it "_#12 may replace this; check_" instead of striking it (D-044 amended).

### Changed
- **Hooks never wait for the database.** They redact, spool locally, start a background drainer and exit. `continuum hook` is dispatched before the CLI loads: median about 100 ms per hook in real Claude Code, from about 730 ms. `SessionStart` drains the spool (up to 8 s) before printing the brief.
- **Setup and the Claude Code plugin** install `PreToolUse`, `PostToolUse`, `PostToolUseFailure`, `SubagentStop` and `PostCompact` hooks for every tool.
- **D-041's one-line chat "actions" are no longer written.** Sessions that have them still show them.

### Fixed
- **App notices are not the user's words.** Background-task notices (`<task-notification>`) and bash-mode blocks were captured as user messages.
- **The end of a long error was dropped.** Errors now keep their head and tail.
- **A cross-app false alarm.** A failure re-run successfully in another app ("Bash" vs "shell") was still flagged.
- **A change of mind in the most recent message** did not mark the requirement it replaced.
- **Review of capture, memory and recall** (D-045). Fixed:
  - An interrupted or timed-out external call (a `git push`) counted as finished. It is now an unknown outcome and blocks external calls.
  - Identical calls from apps without call ids (Gemini CLI, Cursor) collapsed into one record.
  - Refusals used Claude Code's JSON in every app, so Gemini CLI and Cursor ran the call anyway. Cursor's permission hooks now print an explicit allow.
  - Field names were misread: Cursor's `tool_output`, `error_message`, `failure_type`, `duration`, `mcp_server_name`, `workspace_roots` and subagent `summary`; Gemini's `prompt_response`; Claude Code's `mcp_server` and `SubagentHandback`.
  - The deny list stayed after a late result arrived, and was not found from a subfolder.
  - The drainer lock could be taken over by a second drainer while the first was still working. It is now an OS lock.
  - A slow snapshot could delay the refusal past the app's hook timeout.
  - Diffs were stored unredacted. Credential files' content is no longer stored at all.
  - MCP names such as `merge_pull_request` and `checkout_branch` counted as reads. Wrapped commands (`git -C x push`, `bash -c`, `sudo -E`, `npx`, `python -m`) hid an external call.
  - Recall crashed on a stemmed-only match in a long event. It also returned empty hits for transcript copies, matched "or" inside "for", and `until=<date>` dropped that day.
  - `coding_show` treated `%` in a blob id as a wildcard.
  - The agent's question inside an answer could strike a user requirement. A shared number alone ("4000") struck an unrelated requirement.
  - A failing test run disappeared from the brief after 12 other tool calls. A subagent's report was shown under another subagent. MCP write tools could be cut from the brief without a "more" marker.
  - Tool text could close the code span or quote it is shown in.
  - On Windows, `forget` could not delete a snapshot store's read-only git objects (found by Windows CI).
- **Windows: the brief was lost.** The hook printed it in the console's code page (cp1252) and failed on `⚠`. Hooks now always write UTF-8.

## 1.0.0rc1 — 2026-10-19

Release candidate for client delivery: privacy, licensing, portability and packaging.

### Added
- **Erasure and retention (D-039, schema v5).** `continuum coding forget SESSION` and `continuum coding forget --older-than-days N` remove a session's events and row in one transaction and leave a content-free tombstone (`coding_erasures`, immutable). Human-only: CLI with `--yes`, never MCP. `continuum coding erasures` lists them. History outside an erasure stays immutable.
- **Several people, one database (D-040).** Each person connects with their own PostgreSQL role; row-level security shows each role only its own sessions, events, checks and tombstones. Operator commands `continuum admin grant|revoke|reassign|users`. A role without access gets `no_access` naming the fix.
- **What the previous agent did (D-041).** Tool calls in the app's transcript (Claude Code, Codex, Gemini) are kept as one-line actions with a failed mark. The brief lists edited files, recent commands and files read; Attention flags a failing command among the agent's last three. On the real 30-message transcript: 52 of 52 tool calls kept, in order, none duplicated.
- **Benchmark modes `transcript` and `two_apps`.** Turns re-imported from growing app transcripts with tool calls, and a chat that moves between Claude Code and Codex. Every memory kind 98–100% at 10–160 messages; edited files and open failures 100%; no false alarms. Pinned in `test_nf6`.
- **Proprietary licensing.** `LICENSE`, `NOTICE`, SPDX headers in every source file, package metadata (`Private :: Do Not Upload`) and a notice in every document. `tests/unit/test_license.py` keeps it complete.
- **CI on Windows and macOS** in addition to Linux.
- **Trial 6, a real debugging handoff** (`eval/handoff_trials/debug_handoff/`). Model A (Sonnet in Claude Code, hooks only) diagnosed the bug without fixing it; the user's extra facts and constraints were only in the chat. Haiku, Sonnet and Opus each scored 7/7 with Continuum, 4/7 without.
- **Release packaging.** Version 1.0.0rc1. A private release workflow on version tags builds the wheel and sdist, smoke-tests the installed wheel against PostgreSQL, and attaches them with `SHA256SUMS` to a GitHub release (no public index). `SECURITY.md` (reporting, supported versions, controls) and `docs/UPGRADING.md`.
- **Upgrade test from a real 0.9 database** (`tests/fixtures/schema_v4_continuum_0_9.sql`): sessions, chat and checks survive, and erasure, per-person checks and capture work on the upgraded data.

### Fixed
- **A long message starting with "OK" is not an answer.** The user's "OK, I'm out of time. Stop here, …" was shown as the reply to the agent's open question (Trial 6). Only short messages count as replies now.
- **`continuum doctor` on a coding-only install.** It reported failure because no runtime tasks were registered. Only task files that do not load are a fault now.
- **Answers across apps.** A "yes" was paired with the question before it in journal order, so a transcript imported later (another app) could lose the pairing. It now uses conversation order.
- **Repeated words in another app.** The same words from two capture routes were always treated as one message, so a later "yes" typed in another app vanished from the brief. They are one message only when captured within 30 seconds.
- **Windows:** CLI output into a pipe the reader already closed raised `OSError: Invalid argument` (Windows' form of a broken pipe). It is now handled like a broken pipe.

## 0.9.0 — 2026-10-18

Long chats and every model (D-037). Measured in `docs/MEMORY-QUALITY.md`.

### Changed
- **The brief consolidates the whole conversation** (`coding/memory.py`). It no longer shows the first message and the newest six.
  - Every captured message is read. Pasted output is removed.
  - Requirements, changes of mind, answered questions and other statements are ranked, kept within a budget that grows slowly with the chat, and shown in order with message numbers.
  - Measured in 40-message chats: the share of the user's explicit requirements that reach the next model rose from **15% to 100%**. Changes of mind went from 23% to 100%, answers from 0% to 100%, and leaked log noise from 11% to 0%. The brief is ~1.2k tokens.
- **The agent's reported choices and caveats** (from captured reports) are in the brief as claims.
- **The user's most recent message is labelled as such,** not as "the current instruction". When it ended the chat ("stop here, the next model will do it"), Attention says the stop applied to the previous agent.
- **New rule:** keep going with the user's instruction. Do not stall on the previous agent's open questions; pick the safest option and record it. This was found with Haiku.
- **Attention flags a session idle for more than 3 days.**

### Fixed
- **Everything the agent writes is kept**, not only its final message per turn: every agent text message in the transcript (tool calls and tool output excluded). On the real 30-message transcript the journal had 27 of Claude's 47 messages. It now has 47 of 47, plus your 30 of 30, none duplicated, in order, also when the hooks fail and catch up later (`replay_capture.py`, `test_s14`).
- **Exact message identity.**
  - Transcript messages are keyed by the transcript's own ids.
  - Claude Code's uuid-less request/cache copies (`api-request-blob`) are no longer read as messages.
  - A live-captured message and its transcript copy are recognised as one.
  - Recovered messages are ordered by their transcript timestamps.
- **Caveats** such as "Note: X is not handled yet" are recognised in the agent's reports.
- **Repeated messages.** A second identical message in one chat ("yes", "continue") was dropped at capture as a duplicate. Keys now count occurrences: submitted prompts are all kept, and transcript re-imports still add nothing.
- **Duplicate capture at turn end.** A hook payload carrying both a transcript and this turn's fields could store the turn twice. The turn's fields are now skipped when the transcript already ends with them.

### Added
- **`continuum_lab.memory_bench`:** a long-chat memory benchmark.
  - It plants requirements, changes of mind, answers, agent decisions and noise in conversations of 10–160 messages.
  - It includes held-out phrasing sets and compares v0.8.0 with the current version.
  - `test_nf6_long_chat_memory_quality` pins the results.
- **Trial 5:** a real 30-message Claude Code chat, continued by Haiku, Sonnet and Opus. Each scored 13/13 with Continuum and 9/13 without.
- **Claude Code plugin commands** `/continuum:handoff` (record decisions, checkpoint and next step before switching) and `/continuum:resume`.
- **`docs/MEMORY-QUALITY.md`:** how well memory works, model differences, the model-switch notice, limits.

## 0.8.0 — 2026-10-18

Continuity across **apps**, **machines** and **abrupt ends**, not only across models in one app (D-036). The complete algorithm, its bounds and the scenario matrix are in `docs/MEMORY-ALGORITHM.md`.

### Added
- **Chat capture without any model.** `continuum hook <app>` journals the user's own words and each turn's final agent report from the app's hook payload.
  - Claude Code: the plugin's `hooks/hooks.json`, or `continuum coding setup`.
  - Codex: `notify = ["continuum", "hook", "codex"]`.
  - Any app with JSON hooks; `continuum coding import <transcript>` and `continuum coding capture` for others.
  - It works even if the agent never calls a Continuum tool or is cut off mid-turn. In a git repository, the first message starts a session. A plugin's hooks run in every folder, so other folders need an explicit `coding_start`.
  - At a turn's end only the newest 2 MiB of the transcript are read, so long chats do not slow every turn down. Compaction and session end re-read it all to catch up.
  - New event `ChatCaptured`; schema v4, upgraded in place.
- **Structural transcript parser.** It reads Claude Code, Codex, Gemini and generic chat exports, and wrappers it has never seen, by structure. Harness-injected text (system reminders, environment context, instruction files) is removed.
- **The brief leads with "What the user asked"** (verbatim; first + newest 6; newest wins), the **last agent report** (a claim) and **Attention** lines computed from the journal.
- **Brief at chat start.** Claude Code's SessionStart hook puts the brief into the new chat before the model's first turn.
- **`coding_recall`** (MCP, read-only) and `continuum coding recall`: keyword search over everything the session remembers. The brief stays bounded, and nothing is forgotten. 20 MCP tools.
- **`continuum coding setup`.** One command writes the rules into AGENTS.md (imported by CLAUDE.md), Claude Code hooks and `.mcp.json`, idempotently, keeping existing content. It prints the Codex snippet.
- **Code map outside git**, by modification time.

### Changed
- **A session is found by the git top level** (an agent in `repo/src` joins `repo`'s session), then by the **normalized remote URL and the same branch** in another checkout (laptop → container). The session moves there, recorded in the journal, and the human's checks apply there. Another branch is never joined by guessing.
- **Redaction also removes well-known credential formats**: Anthropic/OpenAI, GitHub, GitLab, AWS, Slack and Google keys, JWTs, private keys, and `password=…`-style assignments. This holds even when the secret is not in the environment, because captured chat contains what users paste.
- Agent notes in the full JSON brief are bounded like the Markdown ones. The agent list is bounded.

### Fixed
- `continuum … | head` printed a `BrokenPipeError` traceback.
- **Trial harness leak.** The project copy contained `docs/HANDOFF-TRIALS.md`, which names the hidden chat-only requirements. A baseline agent read it and "knew" them. `setup.sh` now strips the trial write-up and every pointer to the trial, and refuses to start if the requirements appear anywhere in the project.
- **Trial judge.** R4 compared exact JSON text, so compact separators failed it although the keys were sorted. It now checks key order at every level.
- An untracked multi-gigabyte file was read whole to build the code map; it is now scanned with a bounded prefix.

## 0.7.0 — 2026-10-14

Measured, then improved (D-034). All numbers are on a 20,000-file git repository with 200 changed and untracked files.

### Changed
- **The MCP coding tools return a compact brief by default.** `coding_resume`, `coding_start` and `coding_brief` return the Markdown brief plus the task ids, about 1.3k tokens. `detail="full"` returns every field as JSON. Previously the full JSON *and* the same content as Markdown were sent on every handoff (about 8.3k tokens for a 30-note session).
- **The brief is bounded however long the session runs.** It keeps the newest 8 decisions, 6 context notes, 8 blockers, 3 next steps and 5 checkpoints. Notes reach the next model whole (up to 1,000 characters in Markdown, 600 for context), so the *why* of a decision is never cut mid-sentence. File lists are capped. Every omission is counted (`agent_notes.omitted`), and the journal keeps everything. The compact brief stays under 4k tokens and the full JSON under 10k in the worst case; a test doubles the session length and asserts the brief does not grow.

### Added
- **Code map: `verified_facts.session_changes`.** The files changed since the session began (committed, staged, unstaged or untracked), each with +/- lines, git's enclosing-function context and the names defined in added lines. The Markdown brief shows it under "Code touched in this session", and the rules tell the next model to start there instead of re-reading the repository. In the realistic trial it cut the next model's work tokens by about 20% (17.3k → 13.8k) with the same 4/4 result.
- **`verified_facts.commits_since_last_checkpoint`**, observed from git: the next model sees exactly which commits were made since the last checkpoint. When history was rewritten the value is `None` (unknown), never a guess.

### Validated with real model switches
- **Trials.** Sonnet → Opus handoffs on a copy of this repository, judged by hidden tests (`docs/HANDOFF-TRIALS.md`).
  - **Realistic trial:** prior work committed, and requirements stated only in the earlier chat. With Continuum, Opus finished the job (4/4) and honoured both chat-only requirements, using 71.3k tokens. Without it, Opus spent 75.3k tokens and delivered the wrong thing (1/4).
- **Defects the trials found (all fixed, with tests):**
  - redaction turned every `continuum` in paths and commands into `[REDACTED]`, because the default DSN password is that word
  - decision reasons were clipped mid-sentence
  - every commit falsely flagged verified work as changed. The fingerprint is now the content tree, not the commit; this costs about 165 ms on a 20k-file repo
  - in a fresh repo, newer git (2.55) failed `git add --update -- .`

- **The realistic trial is packaged in `eval/handoff_trials/`:** setup, prompts, hidden tests, judge, and a GPT → Claude runbook (Codex CLI → Claude Code). The cross-vendor run awaits OpenAI access.

### Performance
- **Fingerprint:** 7 → 5 git processes. It was about 2× faster before the content-tree change above, which trades that speed back for accuracy. Untracked files now come from the single `git status` call, and the index is listed only when `.gitmodules` exists. A non-functional test pins the git-call count. Measured on the 20k-file repo: a fingerprint takes about 165 ms with the content tree. Accuracy over speed: we chose the content tree over a 70 ms commit-based fingerprint that raised false alarms.

## 0.6.3 — 2026-10-14

This release adds a portability pass and a second full-codebase audit. The portability pass ran the built wheel in a clean environment, launched like an IDE does. The audit found 17 new defects, and the portability pass found 4 more. All are fixed, each with a regression test written first (`tests/integration/test_pass2_hardening.py`, `tests/integration/test_portability.py`; D-033).

### Fixed: any agent, any IDE, any OS, local and cloud
- **Windows.**
  - The CLI crashed with `UnicodeEncodeError` when an agent read it through a pipe (cp1252). Output is now always UTF-8.
  - Checks like `npm test` failed (`npm.cmd` cannot start without a shell); commands are now resolved via PATH/PATHEXT.
  - The lab's mock services imported POSIX-only `fcntl`.
  - Config was read with the ANSI code page; it is now UTF-8 everywhere.
- **IDE launch directory.** State and config defaulted to the server's working directory (Claude Desktop uses `/`). They now default to `~/.continuum`.
- **Cloud.** `setup.sh` used system `pip`, which PEP 668 images refuse and which needs Python 3.12. It now uses `uv tool --python 3.12` and links `continuum` onto PATH.
- **Configs.**
  - Shipped IDE configs had `/absolute/path` placeholders.
  - The Claude Code project config now falls back to the local DSN.
  - Configs added for Windsurf, Gemini CLI and Cline.

### Fixed: correctness and safety
- **Model-controlled input crashed the step loop instead of being rejected.**
  - A path escape in `csv.normalize` `source` or `store.put` `artifact_ref`.
  - NUL characters in tool arguments.

  Both are now `ActionRejected` (`path_not_permitted`, `invalid_args`).
- **NUL characters** in coding notes, goals or binary check output made every journal write fail. They are stored as U+FFFD.
- **Fingerprint blind spots** (fail-open):
  - edits inside a dirty git submodule (now fingerprinted recursively)
  - untracked files with non-UTF-8 names (hashed as raw bytes; such names, or a symlink target, also crashed observation)
- **Projection/replay equivalence (I2).** Float-noise costs (`3.0000000000000004e-06`) never matched the `NUMERIC(20,10)` row, so every resume rebuilt the projection and `inspect` reported it inconsistent. Costs are now quantized to the column's precision.
- **A human `abort` on an ended run** rewrote it from cancelled to failed. Terminal runs are never rewritten.
- **Checks.**
  - Checks received `CONTINUUM_*` credentials. Agent-written test code could reach the journal's database and register checks.
  - Database URL passwords were not redacted from check output.
  - On timeout only the direct child was killed, so grandchildren kept changing the tree. On Windows, captured pipes could hang. Each check now runs in its own process group, with output written to a bounded file.
- **Lease identity.** Default owner ids could collide between containers with the same hostname and pid, which defeated fencing. They now include a per-process random nonce.
- **Budgets.** Active time skipped recovery work and truncated long iterations to the 60 s cap. It is now recorded around every model call and dispatch batch, and at the end of each operation.
- **Crashes.**
  - Undecodable or duplicate task files took down every command; they are now reported by `doctor`.
  - A mixed-case `CONTINUUM_DB_SCHEMA` broke `db-init`; schema names are validated.
  - Bad `--log-level` values and unexpected errors produced raw CLI tracebacks; they are now masked.
  - A missing `[live]` extra crashed runs; it is now a `ModelError`.
- **Verifier messages** were stored unredacted and unbounded, and shown to the model. They are now redacted and limited to 500 characters.

## 0.6.2 — 2026-10-14

The full-codebase audit, continued into the core runtime and interfaces, found 9 issues. All are fixed, each with a regression test written first (`tests/integration/test_core_hardening.py`, D-032).

### Fixed
- **A transient read error permanently failed runs (high).** On resume or finalize, a verifier `ERROR` (for example a `store.head` timeout) was treated like `FAIL`. It invalidated the milestone and could end the run with `drift_requires_remediation`. Now an error records `VerifierError`, invalidates nothing, and pauses in `awaiting_reconciliation`. The next resume re-observes.
- **Concurrent operations in one process shared a lease (high).** Two HTTP requests in one `continuum serve` used the same controller id, so a second resume, resolve or cancel re-entered the live lease with the same epoch. Its `release` then expired the lease under the running driver. Runs are now also claimed per process for each operation, so a concurrent operation gets `already_owned`.
- **DNS rebinding against the loopback HTTP API (medium).** Requests whose `Host` or `Origin` is not loopback are refused.
- **Unknown model alias (medium).** It is rejected before any run is created (start) or any lease is taken (resume), and HTTP returns 422 instead of 500. Previously an orphan run was left `running`.
- **Raw tracebacks from interfaces (medium).**
  - Malformed task files are reported (`continuum doctor` → `invalid_files`) instead of breaking every command.
  - Bad factories and a broken `models.yaml` are typed `invalid_spec` errors.
  - Read-only inspection never loads live models.
  - MCP masks unexpected errors (`code: internal`), and `list_runs` tolerates an unreadable run.
  - The CLI reports `database_unavailable` (bounded by `CONTINUUM_DB_TIMEOUT_SECONDS`, default 10).
- **Costs (medium).** A model-registry cost of 0 is now unknown, never "free". A configured price snapshot replaces LiteLLM's generic estimate.
- **Secrets in rejection reasons (low).** Schema and path errors quoting argument values are redacted before they are persisted.
- **Malformed provider responses (low)** become a typed `ModelError`, so the run hands off instead of crashing.
- **Blob references (low).** They are validated as `blob:sha256:<64 hex>`. A human receipt (`receipt:123`) is never read as a blob, and a nonexistent blob is refused as evidence.

## 0.6.1 — 2026-10-14

A full-codebase audit of the coding-continuity layer found 23 issues, all fixed with regression tests (`tests/integration/test_coding_hardening.py`, D-031).

### Fixed
- **Verification could fail open.**
  - A git command that failed or timed out was hashed as empty, so a dirty tree could match a clean one. Observation now raises `observation_failed`.
  - External diff drivers and textconv could hide edits. Diffs are now raw.
  - In a repo with no commits, staged changes were invisible.
  - `src/build/`-style source directories were excluded at any depth.
  - Untracked symlinks were followed outside the repo.
- **Atomicity:** `coding_start` now writes the session row, `SessionStarted` and the initial tasks in one transaction, after validating every task. Invalid tasks no longer leave orphan sessions or turn `"pytest"` into checks `p, t, s, …`.
- **Evidence race:** `coding_complete_task` is refused if the journal moved on, or the code changed, between evaluation and commit.
- **Closed sessions** no longer run a check's command.
- **Crashes:** a non-executable check, or one whose argv names a directory, is recorded as a failed run (exit 126), not a raw exception.
- **Claims vs facts:**
  - checkpoint summaries moved from `verified_facts` to `agent_notes`
  - agent text is flattened in the Markdown brief, so it cannot forge headings or "verified" lines
  - agents cannot record actions as `human:`
  - agent labels are redacted
- **Containment:** a coding repository is never `/` or the home directory itself, and stays inside `CONTINUUM_CODING_ROOTS` when that is set.
- **Schema:** older code never downgrades a newer schema (`schema_too_new`). Concurrent first-use migrations are serialized by an advisory lock.
- **CLI:** `--` is special only for `coding checks add`, so `note … -- "-x text"` works again.
- Rename entries are parsed correctly in `changed_files`. Timestamps are compared as datetimes. A check that modifies the tree is flagged (`modified_tree`). Briefs observe the repository once and index check runs in a single pass.

## 0.6.0 — 2026-10-14

Spec 0.5.

### Added
- **Coding-session continuity** (D-028, D-029; docs/01 R13/U8, docs/02 §10, docs/03 schema v3). Coding work carries over when you switch models, versions, vendors or chats:
  - an append-only coding journal per repository
  - code state observed by Continuum
  - tasks verified only by human-registered checks passing on the current code with an unchanged definition
  - a handoff brief that separates verified facts from agent claims
  - tamper evidence for check definitions
- MCP: 11 `coding_*` tools (no check-registration or human-decision tool). Server instructions tell agents to call `coding_resume` first.
- CLI: `continuum coding …` for humans (checks, sessions, brief, close) and for shell-only agents (resume, note, checkpoint, check, task).
- `integrations/AGENT_RULES.md` (drop-in rules for AGENTS.md/CLAUDE.md), the Claude Code project `.mcp.json`, the cloud setup script, and `docs/CODING-HANDOFF.md`. README quickstart.
- Automatic schema migration on start (`ensure_schema`, D-030).

### Fixed
- Running a check that wrote `__pycache__` changed the code fingerprint and made the result unusable as evidence.
- `coding checks add … --actor X -- cmd` lost `--actor` to argparse `REMAINDER`.
- The CLI failed with a missing table on a database created by an older version.

## 0.5.0 — 2026-10-13

### Added
- **Schema v2** (D-025):
  - CHECK constraints enforce status validity, I3 (verified ⇒ evidence), confirmed ⇒ outcome, attempt and run consistency, well-formed digests and non-negative usage
  - `TRUNCATE` on `events` is blocked
  - lookup indexes
  - `schema_meta` version
  - idempotent upgrade through `db-init`
- **Verification** (D-026):
  - requirements traceability matrix enforced by a test
  - Hypothesis stateful tests across connector modes
  - non-functional tests (NF1–NF5)
  - CI runs every layer
- `docs/OPERATIONS.md` explains how Continuum runs (no always-on server; PostgreSQL is the only service), how MCP connects over stdio, configuration and the schema design. `docs/VERIFICATION.md` covers the strategy, the defects found and the results.

### Fixed
- **Lease expiry could arrive up to one second early.** DB time was cast with `::bigint`, which rounds to nearest. Leases now use fractional seconds (`DOUBLE PRECISION`, migrated idempotently). This was found when a chaos test failed under load, then reproduced deterministically before fixing (D-027).
- Starting a run with an existing id now raises a typed `run_exists` error instead of a raw database error.

## 0.4.0 — 2026-10-13

### Fixed
- **Live runs on OpenAI and Anthropic would have been rejected:** tool names such as `store.put` violate their `^[a-zA-Z0-9_-]{1,64}$` rule. Names are now mapped bijectively and back. Generated tool-call ids are now paired with their results (D-023).

### Added
- **Model switching across versions, vendors and chats** (D-024):
  - resume a run on any `provider/model-id` (e.g. `anthropic/claude-opus-5-5`) without editing config
  - `profile: auto` in `config/models.yaml`
  - registry profiles accepted only when complete; zero price means unknown
  - LiteLLM `drop_params` for model-family parameter differences
- Contract tests against OpenAI and Anthropic request rules:
  - OpenAI → Anthropic mid-run
  - GPT-5.6 → 6.1 across chats
  - Claude Opus 4.8 → 5.5 by id
  - three-vendor round trip
- Cross-chat MCP test: separate server processes share and resume the same run.
- `docs/MODEL-SWITCHING.md`.

## 0.3.0 — 2026-10-13

Spec 0.4.

### Added
- **MCP plugin for coding agents.** The stdio server now has 8 tools: `list_tasks`, `run_task`, `resume_run`, `cancel_run`, `get_status`, `inspect_run`, `pending_decisions` and `list_runs`. It also gives agents server instructions and creates its tables automatically on start. Human decisions are never exposed (D-022).
- `integrations/` covers the following, with an end-to-end stdio test:
  - a Claude Code plugin with a skill, plus `.claude-plugin/marketplace.json`
  - configs for Cursor, VS Code, OpenAI Codex CLI and Claude Desktop
- `continuum doctor` checks the database, schema, registered tasks and live model profiles. `Continuum.pending_decisions` is a new SDK method.
- **Seamless handoff** (D-020, D-021):
  - capability-aware fallback; a context overflow prefers a larger window
  - handoff provenance in the packet
  - provider-tokenizer packet sizing (`LiteLLMAdapter.count_tokens`)
  - cost estimated from the price snapshot when the provider doesn't report it

### Changed
- `runtime.py` is split into `journal`, `budgets`, `verification` and `sessions` components (D-019). Behaviour is unchanged. `run_coro` moves to `continuum.models.base`.
- Spec 0.4: 00/01/02/05/06 and AGENTS.md describe the MCP surface and the handoff selection rules. The docs/05 packet example adds `handoff`.

## 0.2.0 — 2026-10-13

### Added
- Structured JSON tracing (`continuum.core.tracing`) covering committed events, model calls and tool dispatches (ids, statuses and durations only). The CLI flag `--log-level INFO` streams it to stderr.
- Read-only `inspect` timeline and `trace` export (`continuum trace`, `GET /runs/{id}/trace`, `Continuum.trace`): events as JSON lines, with redacted blobs inlined.
- Background lease heartbeat (`LeaseKeeper`; SDK interval is TTL/3), so a long model or tool call keeps ownership.
- Demo tasks `demo_ack_loss_recovery` and `demo_opaque_needs_human`, plus a `script` option for lab task bindings.
- Lab metric `clean_completion` (completed with no duplicate or unintended effect), with a paired difference.
- Project journal: `docs/PROJECT-JOURNAL.md`.

### Changed
- Drift that would require re-sending confirmed operations now stops the run at once with `failure_code = drift_requires_remediation` (docs/02 §7, docs/03 §5). Previously it looped until models were exhausted.
- The registered task id is stamped onto the run's spec, so resume always finds the binding that started the run.

## 0.1.0 — 2026-10-13

First implementation of the v1 MVP (spec 0.3).

### Spec changes (0.2 → 0.3)

- **Durable backend: PostgreSQL instead of SQLite** (`DECISIONS.md` D-001). Amended 00, 01, 02, 03, 04 and 06, plus `AGENTS.md`. docs/03 embeds the executable DDL, and `tests/unit/test_docs_sync.py` keeps it in sync with `src/continuum/core/schema.sql`.
- docs/05 packet example now shows the implemented fields: `builder_version`, optional `pending_milestones[].description` (removed first under compaction), the full `remaining_budget` including `cost_has_unpriced_usage`, and labelled `data_refs`.

### Added

- `continuum.core`: typed task spec and state enums; typed event payloads; a PostgreSQL store with atomic event+projection transactions and fencing; leases with epochs; action identity and state machine; the reconciler; evidence, workspace and blobs; secret redaction; the deterministic packet builder; and the run controller (start, resume, resolve, cancel, inspect).
- `continuum.tools`: trusted manifests, registry and the single dispatch gateway. Trusted adapters: `file.read`, `csv.normalize`, `report.write`, `store.put`/`store.head`, `db.update`/`db.get`, `message.send`/`message.query`.
- `continuum.verify`: verifier registry and built-in verifiers.
- `continuum.models`: provider-neutral contract and error taxonomy, deterministic fake adapters, and the LiteLLM adapter.
- `continuum.interfaces`: SDK, CLI (`continuum`), loopback FastAPI app and the stdio MCP facade.
- `continuum_lab` (`eval/`): ground-truth mock services (deduped, observable and opaque modes, with scripted faults), 15 tasks, the plan-following fake agent, the B0 baseline, fault injection, the matrix runner (`continuum-lab`), metrics (Wilson and paired bootstrap) and the Markdown report.
- Tests: unit and property, integration, contract (live tests opt-in) and chaos (every crash boundary; real-process lease races and hard kills).
- CI workflow with a PostgreSQL service, ruff, mypy `--strict`, all test layers and a lab smoke run.

---

_Continuum is proprietary and confidential. © 2026 the Continuum copyright holder. All rights reserved. See [LICENSE](../LICENSE)._

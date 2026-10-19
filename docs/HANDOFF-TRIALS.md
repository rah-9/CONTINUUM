# Handoff trials: does a model switch keep the coding work?

The question is not speed. It is whether the next model continues **correctly**, without
re-reading the codebase and without losing what was decided in the previous chat, so the
user doesn't pay twice. These trials measure that with real models. They use a copy of
this repository (52 source files) as the project and drive Continuum through its CLI (the
same protocol as the MCP tools). Hidden acceptance tests outside the project judge the
result. Neither model can see them.

## Method

1. **Model A (Sonnet)** gets the user's full request in its chat. It starts a Continuum
   session, does part of the work, proves it with a human-registered check, and stops.
   This simulates the switch. A is not told what to hand over; it only has
   `integrations/AGENT_RULES.md`.
2. The repository is snapshotted. **Model B (Opus)** then continues in a brand-new chat in
   two conditions, on identical code:
   - **with Continuum:** B follows the agent rules and starts with `continuum coding resume`;
   - **baseline:** same repository, same instruction, no Continuum.
3. **Judged:**
   - the hidden acceptance tests
   - a hidden judge for requirements that existed **only in A's chat**
   - whether B redid finished work
   - tokens and tool calls (from the agent runtime's usage report)
   - files read (B's report, checked against its changes)

## Trial 1: redaction hardening (easy handoff)

A implemented step 1 of 3 and left it **uncommitted** in a single file. Its design decision
was visible in that code.

| B (Opus) | With Continuum | Baseline |
|---|---|---|
| hidden tests | 7/7 | 7/7 |
| followed A's decision (`[REDACTED:jwt]`) | yes | yes |
| redid step 1 | no | no |
| tokens / tool calls | 65.4k / 10 | 68.3k / 10 |

**A tie.** `git diff` was a sufficient handoff. Continuum added no cost and no harm.

## Trial 2: session export (realistic handoff)

A implemented Part 1 and **committed** it. The user's chat also contained two requirements
for Part 2: "never overwrite an existing `--out` file unless `--force`" and "write every
JSON line with sorted keys". B was told only: *"Continue where we left off on the
session-export feature."*

| B (Opus) | With Continuum | Baseline |
|---|---|---|
| finished the remaining work (Part 2 CLI) | **yes** | **no**: did not know it existed |
| hidden acceptance tests | **4/4** | 1/4 |
| chat-only requirement: no overwrite without `--force` | **honoured** | missing |
| chat-only requirement: sorted keys | **honoured** | missing |
| tokens / tool calls | **71.3k / 13** | 75.3k / 15 |
| files read | 4 | 12+ plus repo-wide greps |

Without the handoff, the stronger model spent more and delivered the wrong thing. It wrote
tests and docs for the finished Part 1 and concluded that adding a CLI "would be guessing".
With the handoff it knew what was done (verified on the current code), what remained,
and both requirements that never appeared in the code.

## Trial 3: the same realistic handoff with a code map in the brief

The brief now also lists, observed from git with no model involved, the files the session
changed since it started and the names they define. A rule tells the next model to start
there. The experiment was repeated from scratch: a new Model A run, then B in both
conditions. Model A peeked at a hidden test file, which only affects A's side. Both B
runs were blind to the hidden tests.

## Reading the token numbers correctly

An agent that does nothing still costs **about 54k tokens**: its system prompt and tool
definitions. This was measured with an Opus agent told to reply "OK". The comparable cost
is therefore the **work tokens** (total minus 54k), and what matters is the **cost to a
correct result**.

| B (Opus), realistic handoff | correct? | work tokens | tool calls | files read |
|---|---|---|---|---|
| Continuum, brief without code map (Trial 2) | **4/4** | ~17.3k | 13 | 4 |
| Continuum, brief with code map (Trial 3) | **4/4** | **~13.8k** | **10** | **3** |
| baseline, run 1 (Trial 2) | 1/4 | ~21.3k | 15 | 12+ |
| baseline, run 2 (Trial 3) | 1/4 | ~29.4k | 18 | 14+ |

**Neither baseline run reached a correct result.** Both built tests for the finished part,
and did not know Part 2 or its two requirements existed. One concluded that adding a CLI
"would be guessing"; the other asked whether one was wanted at all. Getting a correct
result therefore needs a second turn in which the user re-explains the work, plus roughly
the ~14k of work Continuum's run spent. That puts the baseline's cost to a correct result
at about **35–45k work tokens, versus ~14k with Continuum: about 60–70% fewer**. It also
saves the user's time repeating themselves.

**Worst outcomes** (what Continuum must never do):
- **Make the next model wrong.** A misleading signal such as a false "re-verify", or corrupted text.
- **Make it more expensive.** A long, noisy brief.

The trials found and fixed one of each kind (see below). The current brief costs about
1–3k tokens of the work budget.

## Trial 4: switching apps after an abrupt end, with an agent that never used Continuum

The hardest realistic case (v0.8.0). Unlike Trials 1–3, nothing depends on the first agent's
discipline:

- **Model A (Sonnet) in real Claude Code**, non-interactively (`claude -p`). Only Continuum's
  Claude Code **hooks** are installed: no rules, no MCP server, no mention of Continuum in
  the prompt. The user's request comes in two turns. The second turn adds two requirements
  for Part 2 (no overwrite without `--force`, sorted keys) and ends the chat: *"I'm out of
  time with you now, stop here."* A never called a Continuum tool. It saved the requirements
  in Claude Code's own per-project memory, which another app cannot see.
- **Model B (Opus) configured like another app** (Codex-style): the repository's `AGENTS.md`
  rules and the MCP server, **no hooks** and no brief at session start. B's prompt does not
  mention Continuum: *"The user switched coding apps in the middle of their work… Continue
  where we left off on the session-export feature. Finish it and commit with git."*
- **Baseline B:** the same code and prompt, no Continuum.
- During both B runs, A's Claude Code transcript and memory directory were moved out of
  reach, so neither B could read A's chat from disk.

| B (Opus) after an app switch | hidden tests | chat-only requirements | tool calls | wall time | cost |
|---|---|---|---|---|---|
| **with Continuum** (rules + MCP only) | **4/4** | **2/2** | **16** | **1.8 min** | **$0.56** |
| baseline | 1/4 | 0/2 | 20 | 5.1 min | $0.63 |

With Continuum, B followed the repository's rules on its own: `coding_resume` first. The
brief gave it both of the user's messages verbatim and the code map. It then registered the
tasks, ran the human-registered checks, completed both tasks as verified, and left a
checkpoint and a next step. The baseline found nothing for its folder, implemented a CLI
without `--out`, and ended with: *"If the earlier chat agreed on things that never made it
into the code, such as a `--out` file option, … I couldn't see them."*

**A first run of this trial was discarded.** The project copy still contained this document,
and the baseline read it and "knew" the hidden requirements. The setup script now strips the
trial's write-up and every pointer to it, and refuses to start if the requirements appear
anywhere in the project. In that discarded run, Continuum's B also scored 4/4 under the
corrected judge (below).

## Trial 5: a 30-message chat, continued by three different models

**Setup.**
- The project is a small CLI built from scratch over **30 user messages** (`eval/handoff_trials/long_chat/`).
- **Model A (Sonnet)** worked in real Claude Code, one turn per message, in one chat. Only the hooks were installed, and A never used Continuum.
- Requirements arrive throughout the chat:
  - Ten were implemented by A: storage location, id format and reuse, list markers, priorities with a change of mind, sorting, delete confirmation, title rules, error format, timestamps.
  - Four were for the remaining export work, stated early "for later": the header in message 8, done as yes/no in 15, no overwrite without `--force` in 21, sort order in 27.
- The chat also contains small talk, "continue", questions and a pasted stack trace that says "must".
- The last message ends the chat: *"Stop here and don't start the export; the next model will do it."*

Then **model B** continued, prompted only with *"Continue where we left off"*:
- with Continuum: configured like another app (rules + MCP, no hooks, no brief at session start)
- without Continuum

Each B ran on its own copy of the project and of the journal, and A's chat files were moved
out of reach. A hidden judge (`judge_taskr.py`, 13 tests) checked the result.

| Model B | with Continuum | without |
|---|---|---|
| Haiku | **13/13** (2 runs) | 9/13 |
| Sonnet | **13/13** | 9/13 |
| Opus | **13/13** | 9/13 |

**Without Continuum,** every model kept the ten implemented requirements (they are in the
code) and did not build the export. Each said it could not tell what was planned.

**With Continuum,** the brief was ~2.1k tokens and carried all four export requirements from
messages 8–27, the change of mind on the default priority, and the code map. By the end of the
chat, model A was carrying ~135k tokens of history per call.

**What this trial found:**
- **Defects in the brief:**
  - Haiku's first run scored 9/13. It treated the previous agent's open proposal as a reason to stop, and it read the user's "stop here" (said to model A) as its own instruction. Both are fixed: a keep-going rule, and the most recent message is now labelled as such, with an Attention line when it ended the chat. Haiku then scored 13/13 twice.
  - In real use, a second identical message ("continue") was dropped at capture. Fixed: message keys count occurrences.
- **Defects in the harness:** the judge first assumed a bare `export` prints to stdout, which the user never asked for. It now tests only what was asked (`export --out`).
- **Resilience:** PostgreSQL was down for A's first minutes. The hooks logged timeouts, A was unaffected, and every message was recovered from the transcript afterwards.

## Trial 6: a debugging handoff, continued by three different models

**Setup** (`eval/handoff_trials/debug_handoff/`):
- **The project** is a small invoicing library with one failing test (`test_invoice_eu`).
- **Model A (Sonnet)** worked in real Claude Code over three turns, with only the hooks installed. It never used Continuum.
  - **Turn 1:** find the root cause, don't fix anything. The user adds a fact: the EU feed also writes amounts with a space as thousands separator ("1 234,50").
  - **Turn 2:** the accountant's rule. VAT is rounded half-up on each line, not on the total. Check what the code does today, still without changing code.
  - **Turn 3:** stop here; the next model fixes it all. Keep the signature of `invoice_total(lines, vat_rate)` and add regression tests named `test_regression_*`.
- **A's work:** it diagnosed correctly and changed no code. Its root cause, the VAT analysis and the constraints existed only in its chat.

**Model B** ran in a new chat, in another checkout of the same repository (same remote), with *"Continue the work on this repository."*:
- **With Continuum:** Claude Code's session-start hook put the brief into the chat.
- **Without Continuum:** no hooks.

Each B got its own copy of A's journal, and A's chat files were moved out of reach (leak guard:
none). A hidden judge (`judge.py`, 7 criteria) checked the result:
- the diagnosed bug
- the space format
- existing behaviour kept
- per-line half-up VAT
- the signature
- named regression tests
- all tests passing

| Model B | with Continuum | without | cost with / without | time with / without |
|---|---|---|---|---|
| Haiku | **7/7** | 4/7 | $0.01 / $0.01 | 39 s / 22 s |
| Sonnet | **7/7** | 4/7 | $0.08 / $0.05 | 24 s / 17 s |
| Opus | **7/7** | 4/7 | $0.33 / $0.13 | 68 s / 26 s |

**Without Continuum,** every model found and fixed the visible bug from the failing test. None
knew about the space format, the VAT rule or the test naming, because those were only in the
chat. They were cheaper and faster because they did only part of the job. The rest would cost
the user another explanation, and, unnoticed, wrong invoices.

**With Continuum,** the brief (~1.4k tokens) carried the user's three messages, A's last report
with its root cause and fix plan, and A's commands. Every model fixed all of it and wrote 4–7
regression tests with the requested names.

**What this trial found:**
- **In the brief:** the user's last message ("OK, I'm out of time. Stop here, …") was labelled as a reply to A's open question, because it starts with "OK". Fixed: only short messages count as replies (`test_a_long_message_starting_with_ok_is_not_an_answer`).
- **In the harness:** the first judging ran under a Python without pytest, so "regression tests" and "all tests pass" failed for every run. The runner now uses the project's environment, and all runs were re-judged.

## Trial 7: capture fidelity — does Continuum record everything a real agent does?

**Setup** (`eval/handoff_trials/capture_fidelity/`):
- **Model A** (Sonnet) ran one turn in real Claude Code, with only Continuum's hooks installed.
- **What A did:**
  - ran the failing tests (piped into `tail`, so the exit code was 0)
  - fixed `parse.py` with an edit
  - ran a script that writes `invoicer/rates_table.py`
  - used a subagent to list the TODOs
  - used a project skill (`release-notes`)
  - created a ticket through an MCP server that writes outside the repository
- **The check:** `check.py` compares the journal with Claude Code's own transcripts, read independently of Continuum's parser, and with `git status`. A timing wrapper measured every hook.

| | result |
|---|---|
| tool calls captured (of Claude Code's record, incl. the subagent's own call) | **10/10**, all with a result, 0 status mismatches |
| kinds | 4 built-in, 3 shell, 1 subagent (its call tagged), 1 skill (`release-notes`), 1 MCP (`mcp__tickets__create_ticket`, result `TCK-100`) |
| files changed: git vs. observed by Continuum | **3/3** (`parse.py`, the script's `rates_table.py`, `RELEASE_NOTES.md`) |
| hook time, median (max) | PreToolUse 99 ms (108), PostToolUse 120 ms (311), others 92–146 ms, SessionStart 776 ms |

**Handoff** (`handoff.sh`):
- **Model B** ran in a new chat, in another checkout of the same repository. Neither arm had the tickets MCP server.
- **The question:** which test was failing, which files changed, the ticket id, and which TODOs the subagent found.

| Model B | with Continuum | without | cost with / without |
|---|---|---|---|
| Haiku | **4/4** | 3/4 | $0.003 / $0.006 |
| Sonnet | **4/4** | 3/4 | $0.045 / $0.084 |
| Opus | **4/4** | 3/4 | $0.100 / $0.179 |

**Without Continuum,** every model rebuilt the repository facts from git and grep, at about twice the cost. None could know the ticket id: it exists only in the tracker and in Continuum's journal. The same holds for any effect outside the repository: a push, a deploy, a message.

**What this trial found** (all fixed, with regression tests):
- **Hooks were slow.** They took about 730 ms each: the `continuum` command loaded the CLI and database stack first. It now dispatches hooks first (about 100 ms).
- **A notice became a user message.** A background subagent's notice (`<task-notification>`) was captured as a user message.
- **A subagent's report was missing.** A background subagent's tool result is only a handle; the report comes with `SubagentStop` and is now kept.
- **A failing test run looked ok.** The piped run exited 0; output signals now flag it.
- **"Read" listed a tool search** among the files read.

## Defects the trials found in Continuum (all fixed, with regression tests)

1. **Corrupting redaction.** The README's local database password is the word
   `continuum`. Every path and command in notes and briefs became `[REDACTED]`
   (`src/[REDACTED]/core/…`), and the corruption was stored permanently.
   - **Fix:** URL credentials are hidden inside URLs. Bare occurrences are redacted only
     when the password looks like a secret.
   - Tests: `test_dictionary_word_url_passwords_never_corrupt_ordinary_text`.
2. **Clipped reasons.** Bounding notes at 300 characters cut A's decision *reason* and
   context mid-sentence.
   - **Fix:** the brief keeps fewer notes, but keeps them whole.
   - Test: `test_b4_decision_reasons_and_next_steps_reach_the_next_model_whole`.
3. **False "needs re-verification" after every commit.** The fingerprint included the
   commit id. A ran its check and then committed, so B was told verified work had changed.
   - **Fix:** the fingerprint is the content tree of the working copy, so committing,
     staging or amending identical content is not a change.
   - Test: `test_committing_or_staging_identical_content_keeps_the_fingerprint`.

4. **`continuum … | head` crashed** with a `BrokenPipeError` traceback (agents pipe output
   constantly; found while preparing Trial 4).
   - Test: `test_p3_cli_output_into_a_closed_pipe_is_not_a_crash`.

Defects in the trial harness (fixed): the project leaked this write-up (above), and the R4 judge
compared exact JSON text, so compact separators failed it although the keys were sorted. It
now checks key order.

## Limits

- Small samples: two runs per condition on the realistic handoff, and one clean run of the
  app switch (Trial 4). The direction is consistent
  (Continuum 2/2 correct, baseline 0/2), but the token figures are not a statistic. Widen to
  more task types, and repeat.
- Trials 1–3 used the CLI; Trial 4 used the MCP tools and real Claude Code hooks.
- Trial 4's "other app" is Claude Code configured like Codex (rules + MCP, no hooks), because
  this environment has no OpenAI access. Codex's own `notify` capture is covered by the scenario
  tests, not by a live run.
- Sonnet → Opus within one vendor. The GPT → Claude trial is packaged and ready, but not yet run:
  the cloud environment these trials ran in has no OpenAI access. See
  [`eval/handoff_trials/README.md`](../eval/handoff_trials/README.md); it uses Codex CLI (GPT)
  as model A and Claude Code as model B.
- Token counts include each agent's fixed system prompt and tool definitions, which are the
  same in both conditions. The differences are the work.

## Reproduce

The realistic trial is packaged in [`eval/handoff_trials/`](../eval/handoff_trials/README.md):
setup script, exact prompts, hidden acceptance tests, chat-requirement judge, and a judging
script. It reproduces the verdicts above (`judge.sh` gives 4/4 for the Continuum runs and 1/4
for the baselines).

---

_Continuum is proprietary and confidential. © 2026 the Continuum copyright holder. All rights reserved. See [LICENSE](../LICENSE)._

# How well the memory works: measurements, model differences and limits

This document answers four questions with numbers: How much of a long chat reaches the next
model? Does it work for chats of 30–40 messages and longer? Does it depend on which LLM
continues? What happens when an app shows a "you changed the model" notice or forces a
new chat? Every number below comes from a test or a trial in this repository, and the
method is described so it can be rerun. The algorithm itself is in
[MEMORY-ALGORITHM.md](MEMORY-ALGORITHM.md).

## 1. Short answers

- **Long chats.**
  - **v0.8.0:** kept the first message plus the newest six. In 40-message chats, only **15%** of the user's explicit requirements reached the next model.
  - **v0.9.0:** reads every message. It keeps **100%** of planted requirements, changes of mind, answers and agent decisions in chats of 10–160 messages.
  - **Cost:** 0.7k–3k tokens, against the ~135k-token history a 30-message chat had reached.
- **Real 30-message chat, three models.**
  - **Model A:** Sonnet in real Claude Code. It never used Continuum; only the hooks were installed.
  - **Model B:** Haiku, Sonnet and Opus, each set up like another app.
  - **Result:** all three finished the work, **13/13** hidden tests, including four requirements stated only in messages 8, 15, 21 and 27.
  - **Without Continuum:** each model scored 9/13 and said it could not tell what was planned.
- **Different LLMs.**
  - **Model-independent:** capture, consolidation and verification are plain code, so every model receives the same facts.
  - **Model-dependent:** following the rules and reading the brief correctly.
  - **What the trials found:** the weakest model (Haiku) exposed two places where the brief could be misread. Both were fixed (§4), and Haiku then scored 13/13 in two runs.
  - **Not measured:** GPT models, because this environment has no OpenAI access.
- **Both sides of the chat.** Your messages and everything the agent writes (except tool calls and tool output) are kept exactly. On the real 30-message transcript: 30/30 and 47/47, none missing, none duplicated, in order, even when the hooks failed (§2).
- **The model-switch notice.**
  - Continuum cannot remove an app's own dialog. That dialog is part of the closed-source app.
  - It makes the dialog harmless. Accepting a new chat costs nothing in memory, because the new chat receives the brief automatically in Claude Code, or through `coding_resume` elsewhere.
  - A new chat is also far cheaper than carrying the old chat over to a new model (§6).

## 2. Both sides of the conversation are kept exactly

Your messages and everything the agent writes (its explanations, choices and caveats, not
its tool calls or tool output) are journaled. The real 30-message transcript from Trial 5 was
replayed through the exact hook sequence Claude Code produced
(`eval/handoff_trials/long_chat/replay_capture.py`), in three situations:

| hooks failing (database down) | your messages | Claude's messages | missing | duplicated | order |
|---|---|---|---|---|---|
| never | 30 / 30 | 47 / 47 | 0 | 0 | as written |
| first 4 turns | 30 / 30 | 47 / 47 | 0 | 0 | as written |
| every turn (recovered at session end) | 30 / 30 | 47 / 47 | 0 | 0 | as written |

Before this fix, the same chat kept 27 of 47 of Claude's messages, and only 27 of its 30
end-of-turn replies. The real transcript showed three problems that synthetic tests had not:
- **Only the final agent message of a turn was kept.** Now every agent text message is kept.
- **A catch-up after an outage kept only the very last agent message.** Messages are now matched by the transcript's own ids, so a re-read keeps every missed message exactly once.
- **Claude Code transcripts also contain uuid-less request and cache copies of messages** (`api-request-blob`). When a transcript identifies its messages, records without an id are treated as copies.

A message captured live by a hook and later seen again in the transcript is recognised as
the same message. Recovered messages take the transcript's timestamps, so the brief keeps
the true order, which "the higher number wins" depends on.
`test_s14_both_sides_of_the_chat_are_kept_exactly_through_an_outage` pins all of this.

## 3. Long chats: the memory benchmark

`continuum_lab.memory_bench` generates coding conversations of N user messages, each
followed by the agent's final report, and plants facts at random turns:

- explicit requirements, sometimes buried in small talk
- requirements with no cue word
- changes of mind ("actually, use port B instead of A")
- short answers to the agent's questions ("Should I drop `x_v1`?" → "yes")
- decisions the agent mentions in its reports
- noise: pasted stack traces and log lines that contain "must" and "should"; small talk; "continue"; task steps

Each conversation is captured exactly as the hooks capture it, the real service builds the
brief, and the compact Markdown (what the next model reads) is scored. 20 conversations
per row:

| messages | version | explicit | no cue | fresh phrasings¹ | changes of mind | answers | agent decisions | noise leaked | tokens (max) |
|---|---|---|---|---|---|---|---|---|---|
| 10 | v0.8.0 | 0.74 | 0.80 | 0.46 | 1.00 | 0.00 | 0.00 | 0.42 | — |
| 10 | **v0.9.0** | **1.00** | **1.00** | **1.00** | **1.00** | **1.00** | **1.00** | **0.00** | 778 |
| 40 | v0.8.0 | 0.15 | 0.14 | 0.10 | 0.23 | 0.00 | 0.00 | 0.11 | — |
| 40 | **v0.9.0** | **1.00** | **1.00** | **1.00** | **1.00** | **1.00** | **1.00** | **0.00** | 1,225 |
| 80 | v0.8.0 | 0.07 | 0.08 | 0.05 | 0.18 | 0.00 | 0.00 | 0.02 | — |
| 80 | **v0.9.0** | **1.00** | **1.00** | **1.00** | **1.00** | **0.99** | **1.00** | **0.00** | 1,776 |
| 160, verbose² | v0.8.0 | 0.04 | 0.03 | 0.03 | 0.07 | 0.00 | 0.00 | 0.01 | — |
| 160, verbose² | **v0.9.0** | **1.00** | **1.00** | **1.00** | **1.00** | **1.00** | **1.00** | **0.00** | 3,035 |

¹ **Generalization was measured, not assumed.** Three sets of phrasings were held out of
development:
- The first set was measured, and its misses led to general cue families (preferences, contrasts, statements of fact with a value). After that it no longer counts as held out.
- The second set, written after that change, scored 47–74% on the lexical rules alone. This led to the structural change in §3: keep every statement that is not a question, small talk or pasted output.
- The third set (the "fresh phrasings" column) was written after the last change and measured once.

² **Verbose chats** add paragraphs of discussion that compete for the budget. With a fixed
budget, recall at 160 messages fell to 23–78%. The budget now grows by 50 characters per
message beyond 40 and is capped at about 2.5k tokens of ledger. This is the measured capacity limit.

`tests/nonfunctional/test_nonfunctional.py::test_nf6_long_chat_memory_quality` pins this:
any change that drops recall below 95% or lets noise through fails the build.

## 4. What changed in the algorithm (v0.9.0)

Before, the brief showed the first message and the newest six. Now
`coding/memory.py` reads every captured message (D-037):

1. **Prose only.** Code blocks, stack traces, log lines and diffs are removed, and a pasted
   block continues until a blank line. A "must" inside a log is never a requirement.
2. **Ledger.** Sentences are ranked:
   - changes of mind and answered questions first
   - then strong cues ("must", "never", "only", "at most", …)
   - then weak cues, preferences and specs ("I'd like…", "X, not Y", "Page size: 25")
   - then every other statement that is not a question or small talk

   They are shown in conversation order with message numbers, so "the higher number wins"
   can be applied. Repeats keep their newest position.
3. **Answers.** "yes" is shown together with the question it answered.
4. **Agent decisions.** Choices and caveats from the agent's captured reports are shown as
   claims. A model that never wrote a note still hands over its reasoning.
5. **Budget.** The newest message is always whole. The ledger is filled strongest and
   newest first, and every omission is counted. `coding_recall` finds the rest.

Capture was fixed too. A second identical message in one chat ("yes", "continue") was
dropped as a duplicate. Message keys now count occurrences, so each submitted prompt is
kept, while re-importing a transcript still adds nothing.

## 5. Different LLMs

What does not depend on the model: capture by the app, the journal, consolidation, the code
map, check verification. Every model receives the same facts, in plain Markdown, at the
same size.

What does depend on the model: following `AGENTS.md` (calling `coding_resume`), and reading
the brief the way it is meant. The trials found two places where a weaker model could misread
it:

| Found with | Misreading | Fix |
|---|---|---|
| Haiku, long chat | The previous agent's last report ended with an open proposal. Haiku stopped to ask the user instead of continuing. | Rule: continue with the user's instruction; for an open question the user did not answer, pick the safest option, record it as a decision, and say so. Ask first only for destructive choices. |
| Haiku, long chat | The user's last message to the previous agent ("stop here, the next model will do it") was labelled "the current instruction", so Haiku did not start the remaining work. | It is labelled "the most recent message". When it ends the chat, Attention says the stop was addressed to the previous agent and the remaining work is now theirs. |

Results on the 30-message chat (§7), after the fixes: Haiku 13/13 in two runs, Sonnet
13/13, Opus 13/13. GPT models (Codex) were not run: this environment has no OpenAI
access. The harness is vendor-neutral, and the GPT → Claude runbook is in
`eval/handoff_trials/README.md`.

## 6. The "you changed the model" notice

Coding apps behave differently when you change model mid-chat:
- Some warn you.
- Some start a new chat.
- Some keep the chat and send the whole history to the new model.

Continuum cannot change an app's own dialog. It makes either outcome safe and cheap:

- **If the app starts a new chat:**
  - In Claude Code, the session-start hook puts the brief into the new chat before the first turn, on start, `/clear`, resume and compaction. No action is needed.
  - In other apps, the rules make the model call `coding_resume` first.
- **If the app keeps the chat:**
  - The new model re-reads the whole history, and prompt caches do not carry across models.
  - Measured in the 30-message trial: by message 30, every call carried **~135,000 tokens** of history. The Continuum brief for the same chat is **~2,100 tokens**, so starting a new chat on the new model is the cheaper way to switch.
- **Before a deliberate switch,** run `/continuum:handoff` in Claude Code (a plugin command). The current model records its decisions, a checkpoint and the next step. `/continuum:resume` does the opposite in the new chat.

## 7. Real trials (summary; details in HANDOFF-TRIALS.md)

| Trial | Model A | Model B | With Continuum | Without |
|---|---|---|---|---|
| 2–3: requirements only in chat | Sonnet, uses Continuum | Opus | 4/4, 4/4 | 1/4, 1/4 |
| 4: app switch after an abrupt end | Sonnet in Claude Code, hooks only | Opus, rules + MCP | 4/4 | 1/4 |
| **5: 30-message chat** | Sonnet in Claude Code, hooks only, 30 turns | Haiku | **13/13, 13/13** | 9/13 |
| | | Sonnet | **13/13** | 9/13 |
| | | Opus | **13/13** | 9/13 |

In Trial 5 the database was down for the first minutes of model A's chat, and the hooks
logged timeouts. Every message from that time was recovered from the transcript later: a
real-world check of the "hooks never break the agent, and catch up" design.

## 8. Limits

- **Lexical rules.** Requirements are recognised by language patterns plus "keep every
  statement". A requirement buried in a long question ("could you make sure X?") is a
  question, so it is not in the ledger. It is still in the journal and in recall.
- **Capacity.**
  - **Beyond about 160 messages,** the ledger cap (~2.5k tokens) drops the oldest low-priority statements first, and the brief says how many. Requirements with strong cues are dropped last.
  - **Very long chats** should be closed and restarted per task.
- **English.** The cue lists are English. Other languages keep only the newest message,
  answers, and the statements tier.
- **Small samples.** Real trials are 1–2 runs per arm. The benchmark has 20 conversations
  per row and is deterministic.
- **GPT not measured here** (no OpenAI access in this environment).

---

_Continuum is proprietary and confidential. © 2026 the Continuum copyright holder. All rights reserved. See [LICENSE](../LICENSE)._

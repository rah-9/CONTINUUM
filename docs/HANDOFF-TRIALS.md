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

## Limits

- One run per condition per trial, so the numbers show a direction, not a statistic. Model
  output varies, and the trials should be repeated, and widened to more task types.
- The agents used the CLI, not MCP tools. The protocol is identical, and the MCP path is
  covered by `tests/integration/test_mcp_stdio.py`.
- Sonnet → Opus within one vendor. Cross-vendor handoffs (e.g. GPT → Claude) use the same
  brief and are not yet trialled with live models.
- Token counts include each agent's fixed system prompt and tool definitions, which are the
  same in both conditions. The differences are the work.

## Reproduce

The harness used for these trials is described above. The acceptance tests were plain
pytest files registered as human checks:

```bash
continuum coding checks add <proj> <name> --actor <you> -- \
    env PYTHONPATH=<proj>/src python -m pytest -q <accept>/<test>.py
```

# Context compression on a real session: results

**Question.** When a long coding session moves to a new model or chat, the new model gets
a few thousand tokens of context. Which way of filling them keeps the most of what
matters?

**Method.**

- The session: this project's own development chat. 255k tokens, 726 messages, 1,839
  tool calls, exported verbatim from its transcript (`run_eval.py export`).
- 32 known-answer questions (`qa.json`), spread over the session: early, middle, capture
  work, research, late. Examples: what the user decided, which PR merged what, why
  something failed, what a measurement was. Three questions about private details are
  withheld from the published file. Their keys are in `qa.private.json`, kept outside git.
- Every method gets the same budget: 24,000 characters (about 6k tokens).
- A fresh model (Sonnet) answers all 32 questions from that context alone. With
  "+ search", it may also call `coding_recall` and `coding_show` over MCP against the
  same session imported into Continuum (at most 40 turns). An answer counts when it
  contains the expected keywords; "unknown" never counts.
- Model-built contexts were made with Haiku.

## Results

All search runs below used a copy of the journal cut before the test was written
(`run_eval.py clean-copy`).

| Context given to the new model | Correct | Notes |
|---|---|---|
| **Structured working memory** (a model updates 7 fixed sections, part by part) | **24/32, 25/32** | the best method found |
| The same, with search of the journal | 24/32 | the reader chose not to search |
| Brief's user words + working memory ("combined") | 22/32, 20/32 | |
| The last 6k tokens of the chat | 21/32 | knows only the end |
| Product brief, first build of the memory, with search | 19/32 | 6 turns |
| Anchored memory (itemised facts, merged by rules) | 15/32, 18/32 | the item merging lost detail |
| One-shot summary of the whole chat | 17/32 | |
| Rule-based brief + search of the journal | 17/32 | 14 turns |
| Product brief, first build of the memory | 16/32, 17/32 | its memory alone: 9/32 |
| Hybrid (summary + tail) | 16/32 | |
| Rule-based brief alone (1.0.0rc3) | 9/32 | 0/9 on the capture work, 1/7 on research |

**Withdrawn result.** A first run reported 32/32 for the working memory with search and
31/32 and 29/32 for the rule-based brief with search. The session under test is the one
that built this test, so its journal contained `qa.json` with the answers, and search
could find them. On a clean copy the scores are those above. `clean-copy` now prevents it.

**First product build.** The product's memory scored below the test's own build. Its
size guard cut each section to an equal share from the end, where the model appends the
newest facts: the late questions scored 0/4. Fixed: any overflow is tightened by the
model, and a last-resort cut keeps each section's first and newest bullets. The cap is
24,000 characters, the size the test measured.

**Fixed product build, re-scored:** 12/32 alone, 18/32 with search (memory 22,979
characters, 14 Haiku calls, $0.31). It is still well below the test's own build
(24–25/32), though both read nearly the same history (about 1.05M characters in 11
parts). The likely cause is the size guard. The test's build never tightened: it grew to
38k characters and the reader saw its first 24k. The product's build was squeezed back
under 24k three times along the way, and each squeeze loses detail. Matching the test's
quality is open work.

By part, for the best method and the old default:

| | early | middle | capture | research | late |
|---|---|---|---|---|---|
| structured working memory | 3/7 | 4/5 | 8/9 | 6/7 | 4/4 |
| rule-based brief alone | 4/7 | 2/5 | 0/9 | 1/7 | 2/4 |

## What this means

1. The rule-based brief kept the user's words but did not understand the work. On its
   own it was the worst method.
2. A memory that a model keeps updating in fixed sections was the best context. Updating
   it part by part keeps older facts. Rewriting it in one shot loses them: the one-shot
   summary scored 17/32.
3. No method answered everything. The early part of a long session is the hardest. The
   verbatim journal stays the source of truth and is searchable, but in this test search
   added little: the reader often did not search.

Built into Continuum 1.0.0rc4 as the working memory (D-048, `docs/MEMORY-ALGORITHM.md`
§3.4b).

## Rerun

```bash
export COMPRESSION_TEST_DIR=/some/scratch/dir   # contexts, answers, logs (not in git)
cp eval/compression_test/qa.json "$COMPRESSION_TEST_DIR"/
python eval/compression_test/run_eval.py export <claude-code-transcript.jsonl>
python eval/compression_test/run_eval.py build state      # or tail, summary, anchored, ...
python eval/compression_test/run_eval.py answer state
python eval/compression_test/run_eval.py clean-copy <session id> eval_clean   # then point mcp.json at it
python eval/compression_test/run_eval.py recall state <session id>   # + search
python eval/compression_test/run_eval.py score
```

Model calls use the `claude` command. Set `CLAUDE_WRAPPER` if it needs a wrapper, for
example a proxy.

---

_Continuum is proprietary and confidential. © 2026 the Continuum copyright holder. All rights reserved. See [LICENSE](../../LICENSE)._

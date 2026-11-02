# Context compression on a real session: results

**Question.** When a long coding session moves to a new model or chat, the new model gets
a few thousand tokens of context. Which way of filling them keeps the most of what
matters?

**Method.**

- The session: this project's own development chat. 255k tokens, 726 messages, 1,839
  tool calls, exported verbatim from its transcript (`run_eval.py export`).
- 32 known-answer questions (`qa.json`), spread over the session: early, middle, capture
  work, research, late. Examples: what the user decided, which PR merged what, why
  something failed, what a measurement was. One question about a private detail is
  withheld from the published file. Its keys are in `qa.private.json`, kept outside git.
- Every method gets the same budget: 24,000 characters (about 6k tokens).
- A fresh model (Sonnet) answers all 32 questions from that context alone. With
  "+ search", it may also call `coding_recall` and `coding_show` over MCP against the
  same session imported into Continuum (at most 40 turns). An answer counts when it
  contains the expected keywords; "unknown" never counts.
- Model-built contexts were made with Haiku.

## Results

| Context given to the new model | Correct | Notes |
|---|---|---|
| **Structured working memory + search of the journal** | **32/32**, twice | $0.16 and $0.09 to answer; 9 and 12 turns |
| Rule-based brief + search of the journal | 31/32, 29/32 | $0.27; 21 turns: it has to search for what the brief lacks |
| Structured working memory (a model updates 7 fixed sections, part by part) | 24/32, 25/32 | |
| Brief's user words + working memory ("combined") | 22/32, 20/32 | |
| The last 6k tokens of the chat | 21/32 | knows only the end |
| Anchored memory (itemised facts, merged by rules) | 15/32, 18/32 | the item merging lost detail |
| One-shot summary of the whole chat | 17/32 | |
| Hybrid (summary + tail) | 16/32 | |
| Rule-based brief alone (1.0.0rc3) | 9/32 | 0/9 on the capture work, 1/7 on research |

By part, for the best and the old default:

| | early | middle | capture | research | late |
|---|---|---|---|---|---|
| working memory + search | 7/7 | 5/5 | 9/9 | 7/7 | 4/4 |
| rule-based brief alone | 4/7 | 2/5 | 0/9 | 1/7 | 2/4 |

## What this means

1. The rule-based brief kept the user's words but did not understand the work. On its
   own it was the worst method.
2. A memory that a model keeps updating in fixed sections was the best context on its
   own. Updating it part by part keeps older facts. Rewriting it in one shot loses
   them: the one-shot summary scored 17/32.
3. No compressed context reaches 32/32 alone. The verbatim journal plus search makes
   nothing lost, and a good memory halves the searching needed.

Built into Continuum 1.0.0rc4 as the working memory (D-048, `docs/MEMORY-ALGORITHM.md`
§3.4b).

## Rerun

```bash
export COMPRESSION_TEST_DIR=/some/scratch/dir   # contexts, answers, logs (not in git)
cp eval/compression_test/qa.json "$COMPRESSION_TEST_DIR"/
python eval/compression_test/run_eval.py export <claude-code-transcript.jsonl>
python eval/compression_test/run_eval.py build state      # or tail, summary, anchored, ...
python eval/compression_test/run_eval.py answer state
python eval/compression_test/run_eval.py recall state <continuum session id>  # + search
python eval/compression_test/run_eval.py score
```

Model calls use the `claude` command. Set `CLAUDE_WRAPPER` if it needs a wrapper, for
example a proxy.

---

_Continuum is proprietary and confidential. © 2026 the Continuum copyright holder. All rights reserved. See [LICENSE](../../LICENSE)._

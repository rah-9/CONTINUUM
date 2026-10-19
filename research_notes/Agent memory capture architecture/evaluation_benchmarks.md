# Evaluating long-term memory and long-context handling for LLM agents: benchmarks and measured findings

Scope: which memory strategy works (full context, retrieval/RAG, LLM-extracted memories, summaries/compaction, structured stores), where each fails, and how these are measured. Compiled 2026-10-19.

How sources are labelled: **[PR]** = peer-reviewed (conference, journal or workshop), **[PP]** = arXiv preprint with no confirmed venue, **[V]** = vendor report, blog or vendor-authored paper. Some venues come from the paper's own header or the arXiv metadata, as noted. Others come from background knowledge and are marked "venue not re-verified".

Access note: arxiv.org was blocked from this sandbox by a proxy 403 and a DNS failure, so arXiv full texts were read through the Firecrawl research index (`firecrawl_research_read_paper`). The links below point to the canonical arXiv pages.

---

## 1. Benchmarks: what they measure and their key results (LongMemEval, LoCoMo, MemoryAgentBench, MSC/DMR, BEAM and 2025–26 successors)

### Takeaway
Every major benchmark finds the same pattern. Production "extract facts" memories (ChatGPT, Coze, Mem0, Cognee and similar) lose a lot of accuracy compared with an LLM reading the relevant raw history. Long-context models degrade as history grows. Lossless retrieval over **verbatim, finely decomposed history with extra index keys (facts, timestamps)** plus a structured reading step is the best-measured design. Contradiction resolution and knowledge updates (multi-hop) remain close to unsolved by every strategy.

### Cited Findings

**LongMemEval (Wu et al., ICLR 2025) [PR]**
- Tests 5 abilities: information extraction, multi-session reasoning, temporal reasoning, knowledge updates and abstention. It has 500 curated questions embedded in chat histories whose length can be scaled. — [arXiv 2410.10813](https://arxiv.org/abs/2410.10813) (venue ICLR 2025 per arXiv metadata)
- Two standard settings: LongMemEval_S (~115k tokens per question) and LongMemEval_M (500 sessions, ~1.5M tokens). Grading uses a GPT-4o judge (gpt-4o-2024-08-06) with >97% agreement with human experts. Meta-evaluation accuracy is 0.98 on GPT-4o answers and 0.97 on Llama-3.1-8B answers, and is lowest for abstention (0.90–0.97) and preference questions. — [arXiv 2410.10813](https://arxiv.org/abs/2410.10813)
- **Commercial assistants** (human study, 97 questions, short histories of 3–6 sessions, about 10× shorter than _S, August 2024):

  | System | Model | Accuracy |
  |---|---|---|
  | Offline reading of the full history | GPT-4o | 0.9184 |
  | ChatGPT memory | GPT-4o | 0.5773 (−37%) |
  | ChatGPT memory | GPT-4o-mini | 0.7113 |
  | Coze | GPT-4o | 0.3299 (−64%) |
  | Coze | GPT-3.5-turbo | 0.2474 |

  Failure modes: "ChatGPT tended to overwrite crucial information as the chat continues, while Coze often failed to record indirectly provided user information." — [arXiv 2410.10813](https://arxiv.org/abs/2410.10813)
- **Long-context LLMs, Oracle (evidence sessions only) vs full _S history (~115k):**

  | Model | Oracle | _S | Drop |
  |---|---|---|---|
  | GPT-4o | 0.870 | 0.606 | 30.3% |
  | Llama 3.1 70B | 0.744 | 0.334 | 55.1% |
  | Llama 3.1 8B | 0.710 | 0.454 | 36.1% |
  | Phi-3 14B | 0.702 | 0.380 | 45.9% |
  | Phi-3.5 Mini | 0.660 | 0.342 | 48.1% |

  With Chain-of-Note, GPT-4o scores 0.924 → 0.640 (−30.7%) and Llama 70B scores 0.848 → 0.286 (−66.3%). — [arXiv 2410.10813](https://arxiv.org/abs/2410.10813)
- **Recommended design choices.** The paper frames memory as indexing → retrieval → reading, with 4 control points: value, key, query and reading.
  - *Value granularity (session decomposition):* "round is the more optimal granularity". Replacing sessions or rounds with extracted summaries or facts "negatively impacts QA performance due to information loss". The one exception is multi-session reasoning, where fact decomposition helps. The best token budget depends on the reader model: Llama 3.1 8B "drops sharply beyond 3k retrieved tokens", while GPT-4o "continues to improve even with over 20k retrieved tokens". — [arXiv 2410.10813](https://arxiv.org/abs/2410.10813)
  - *Fact-augmented key expansion:* index each memory under the original text plus the facts extracted from it (K = V + fact). Average gains are +9.4% recall@k and +5.4% final QA accuracy. On LongMemEval_M with value = round, Recall@5 goes from 0.582 to 0.644 and GPT-4o top-10 QA from 0.670 to 0.720. Using facts as the *only* key is worse than using the raw value (Recall@5 0.530 vs 0.582). Session summaries as keys are much worse (GPT-4o top-10 QA 0.252 vs 0.676). — [arXiv 2410.10813](https://arxiv.org/abs/2410.10813)
  - *Time-aware query expansion:* index each value by event date, use an LLM to extract a time range from the query, then filter. Temporal-reasoning recall improves by 11.3% (value = round) and 6.8% (value = session). This only works with a strong extractor (GPT-4o). Llama 3.1 8B "struggles to generate accurate time ranges, often hallucinating", and can lower recall (round Recall@5 0.421 → 0.384). — [arXiv 2410.10813](https://arxiv.org/abs/2410.10813)
  - *Reading:* "Even with perfect memory recall", a poor reading strategy costs up to 10 absolute points. Chain-of-Note combined with JSON-formatted memory items gives the best results, and JSON without Chain-of-Note does not consistently help. Retrieved items are sorted by timestamp before reading. — [arXiv 2410.10813](https://arxiv.org/abs/2410.10813)

**LoCoMo (Maharana et al., Snap/UNC/USC, 2024) [PR – venue not re-verified, reported as ACL 2024]**
- 50 conversations averaging 19.3 sessions, about 300 turns and 9.2k tokens (up to 35 sessions). There are 7,512 QA items: single-hop 2,705, multi-hop 1,104, temporal 1,547, open-domain 285 and adversarial (unanswerable) 1,871. The benchmark also has event-graph summarization and multimodal dialogue-generation tasks. — [arXiv 2402.17753](https://arxiv.org/abs/2402.17753)
- QA F1: humans score 87.9 overall. GPT-4-turbo with a 4k window scores 32.1. GPT-3.5-turbo-16K scores 24.1 / 25.2 / 33.5 / 37.8 at 4k / 8k / 12k / 16k context. Its *adversarial* score falls 13.1 → 8.4 → 6.4 → **2.1** as context grows, against 70.2 for GPT-4-turbo at 4k. The authors' reading: "LLMs can be easily misled into generating hallucinations when they are subjected to long contexts". Temporal reasoning trails humans by 73%. — [arXiv 2402.17753](https://arxiv.org/abs/2402.17753)
- RAG over "observations" (LLM-extracted assertions per turn) beats raw logs by about 5% with top-5 retrieval, but the gain "falters with an increase in the number of retrieved observations". RAG over **session summaries "does not significantly improve the performance despite high recall accuracies … likely due to loss of information during the conversion of dialogs to summaries."** — [arXiv 2402.17753](https://arxiv.org/abs/2402.17753)
- Event summarization uses incremental (recursive) summarization. Best FactScore F1 is about 45.9 (GPT-3.5-turbo). The 16K long-context model is *worse* than the 4K one (precision −3.0%, recall −8.7%). Error taxonomy: missed temporal or causal links, **hallucinated details padded from other events**, misread humour or sarcasm, **wrong speaker attribution**, and trivial dialogue treated as salient. — [arXiv 2402.17753](https://arxiv.org/abs/2402.17753)

**Mem0 paper's LoCoMo results (Chhikara et al., 2025) [V/PP – vendor-authored]**
- LLM-as-judge (J) overall, GPT-4o-mini. Note that **full context still wins on accuracy**:

  | Method | J score | p95 total latency | Context tokens |
  |---|---|---|---|
  | Full-context | **72.90%** | 17.117s | ~26k |
  | Mem0^g (graph) | 68.44% | 2.590s | 3,616 |
  | Mem0 | 66.88% | 1.440s | 1,764 |
  | Zep | 65.99% | — | 3,911 |
  | Best RAG (k=2, 256-token chunks) | 60.97% | — | — |
  | LangMem | 58.10% | 60.40s | — |
  | ChatGPT/OpenAI memory | 52.90% | — | — |
  | A-Mem | 48.38% | — | — |

  — [arXiv 2504.19413](https://arxiv.org/abs/2504.19413)
- On temporal questions, ChatGPT memory's J score is 21.71 "primarily due to missing timestamps in most generated memories despite explicit prompting". — [arXiv 2504.19413](https://arxiv.org/abs/2504.19413)
- Scores for the same systems on LoCoMo vary between papers. A 2026 preprint reports Mem0 at 61.4%, Zep at ~71%, Supermemory at 65.4%, EverMemOS at 94.5% and its own "True Memory Pro" at 93.0%, all with a gpt-4.1-mini answer model. — [arXiv 2605.04897](https://arxiv.org/abs/2605.04897) [PP]. LoCoMo leaderboard numbers therefore depend heavily on the harness.

**MemoryAgentBench (Hu, Wang, McAuley, UCSD; v4 June 2026) [PP – venue not confirmed]**
- 4 competencies: Accurate Retrieval (AR), Test-Time Learning (TTL), Long-Range Understanding (LRU) and Selective Forgetting (SF, also called conflict resolution). Data is fed incrementally as chunks across turns. Datasets: AR uses SH/MH-Doc QA (197k/421k tokens), LongMemEval(S*) (355k) and EventQA (534k). TTL uses BANKING77, CLINC150, NLU, TREC and Movie Recommendation (1.44M). LRU uses ∞Bench-Sum and DetectiveQA. SF uses FactConsolidation-SH/MH (262k; built from MQuAKE edits, with the agent told to prefer later facts). — [arXiv 2507.05257](https://arxiv.org/abs/2507.05257)
- Headline results:
  1. RAG wins on AR.
  2. **Long-context models win on TTL and LRU.** The authors say RAG and commercial memory agents "retrieve only partial information … lacking the ability to capture a holistic understanding".
  3. **On Selective Forgetting, "all methods fail on the multi-hop situation (with achieving at most 28% accuracy)"**, and "only long context agents can achieve fairly reasonable results on single-hop".

  — [arXiv 2507.05257](https://arxiv.org/abs/2507.05257)
- Overall scores:
  - Long-context agents: GPT-5-mini (400K) 60.6, Claude-3.7-Sonnet 49.6, GPT-4o 48.8, GPT-4.1-mini 46.9, GPT-4o-mini backbone 42.2.
  - RAG: BM25 41.5, Qwen3-Embedding-4B 38.2, RAPTOR 27.0, GraphRAG 23.4.
  - FC-MH accuracy: GPT-4o 5.0, GPT-5-mini 28.0.
  - FC-SH: GPT-4o 60.0, GPT-5-mini 78.0, BM25 48.0, embedding RAG 28–29.

  — [arXiv 2507.05257](https://arxiv.org/abs/2507.05257)
- Commercial or LLM-extracted memory performs below its own backbone even at short context. Mem0 scores SH-QA 31/25/25 and FC-SH 22/8/18 at 51k/104k/197k (32k/64k/262k for FC), against GPT-4o-mini at 84/83/64. TTL classification: Mem0 35/37/35/29/26 and Cognee 34–42, against 93/93/87/73/66 for the GPT-4o-mini backbone. Mem0, Cognee and MIRIX "need extremely high resources when constructing the memory". — [arXiv 2507.05257](https://arxiv.org/abs/2507.05257)
- Long-context latency per query: GPT-4o-mini 4.9–5.4s, GPT-4o 17–20s, Claude-3.7-Sonnet 22.7–23.3s. Smaller chunk sizes sharply increase memory-construction time. — [arXiv 2507.05257](https://arxiv.org/abs/2507.05257)

**MSC / Deep Memory Retrieval (DMR), from MemGPT (Packer et al., 2023) [PP – venue not re-verified]**
- MSC has 5 sessions per chat, each about a dozen messages. DMR adds a session-6 question about earlier sessions. The baselines see "a lossy summarization of the past five conversations to mimic an extended recursive summarization procedure", while MemGPT pages through the full history.

  | Model | Recursive-summary baseline | +MemGPT |
  |---|---|---|
  | GPT-3.5 Turbo | 38.7% | 66.9% |
  | GPT-4 | 32.1% | 92.5% |
  | GPT-4 Turbo | 35.3% | 93.4% |

  — [arXiv 2310.08560](https://arxiv.org/abs/2310.08560)
- This is the cleanest number on *recursive-summary loss*: a summary baseline gets about 32–39% of the questions right, while lossless paged retrieval over the same history gets about 93%. DMR is small and short (~1k-token chats per BEAM's comparison table), so it is now treated as saturated. — [arXiv 2510.27246](https://arxiv.org/abs/2510.27246)

**BEAM ("Beyond a Million Tokens", Tavakoli et al., 2025) [PP]**
- 100 conversations and 2,000 validated questions at 128K, 500K, 1M and 10M tokens. It covers 10 abilities: IE, multi-hop, knowledge update, temporal, abstention, preference following, summarization, plus new *contradiction resolution*, *event ordering* and *instruction following*. It is multi-domain (coding, math, health, finance and others). — [arXiv 2510.27246](https://arxiv.org/abs/2510.27246)
- "All methods … perform strongest in abstention and weakest in contradiction resolution." Contradiction-resolution scores are about 0.00–0.05 for vanilla long-context, RAG and LIGHT on Qwen2.5-32B, Llama-4-Maverick, Gemini-2.0-Flash and GPT-4.1-nano. — [arXiv 2510.27246](https://arxiv.org/abs/2510.27246)
- GPT-4.1-nano averages, vanilla → RAG → LIGHT (episodic retrieval + working memory + scratchpad of salient facts): 0.239 → 0.309 → 0.345 at 100K, 0.191 → 0.302 → 0.336 at 1M, and 0.109 → 0.218 → 0.226 at 10M. Temporal reasoning at 10M is 0.000–0.075 for all methods. — [arXiv 2510.27246](https://arxiv.org/abs/2510.27246)
- LIGHT ablation: at 100K, "removing retrieval does not change performance … since the scratchpad alone suffices". Removing working memory slightly *helps* at 500K and 1M. — [arXiv 2510.27246](https://arxiv.org/abs/2510.27246)

**Other 2025–26 benchmarks and results (abstract-level, all [PP])**
- EverMemBench uses multi-party, multi-group chats of >1M tokens with 2,400 QA. "Multi-hop reasoning collapses under multi-party attribution even with oracle evidence (26% accuracy)". "Temporal reasoning fails without explicit version semantics beyond timestamps." — [arXiv 2602.01313](https://arxiv.org/abs/2602.01313)
- "Storage Is Not Memory" argues that "extraction at ingestion is the wrong primitive … content discarded before the query is known cannot be recovered at retrieval time". It keeps verbatim events in a single SQLite file and reports LongMemEval 87.8%, LoCoMo 93.0% and BEAM-1M 76.6% (vs 73.9% Hindsight). — [arXiv 2605.04897](https://arxiv.org/abs/2605.04897)
- ENGRAM uses typed episodic, semantic and procedural records with dense retrieval. It claims state of the art on LoCoMo and "exceeds the full-context baseline by 15 points on LongMemEval while using only about 1% of the tokens". — [arXiv 2511.12960](https://arxiv.org/abs/2511.12960)
- An event-centric EDU memory (short propositions with participants, time and source-turn attribution, "non-compressive") matches or beats strong baselines on LoCoMo and LongMemEval_S with shorter QA contexts. — [arXiv 2511.17208](https://arxiv.org/abs/2511.17208)
- MemoryLACE models explicit merge, supersession and contradiction relations with provenance. It reports the best BEAM and StructMemEval scores among same-backbone systems, with "lifecycle expansion and temporal awareness as the principal contributors". — [arXiv 2609.03201](https://arxiv.org/abs/2609.03201)
- "Evaluating Long-Term Memory for Long-Context QA" finds memory-augmented methods "reduce token usage by over 90% while maintaining competitive accuracy". — [arXiv 2510.23730](https://arxiv.org/abs/2510.23730)

### Inferences
- **Extraction-only memory, meaning replacing history with LLM-written facts or summaries, is the worst-measured strategy.** It loses to offline reading (ChatGPT −37%, Coze −64%), to its own backbone (Mem0 and Cognee on MemoryAgentBench), and to verbatim retrieval (LongMemEval value ablation; LoCoMo summary RAG). Extracted facts help as *index keys* and for multi-session aggregation, not as a replacement for the source.
- Full context is the accuracy ceiling only at short to moderate length (LoCoMo ~26k: 72.9% vs 66.9% for Mem0). Its accuracy falls at 100k+ and its cost and latency grow with length.
- The most promising design is lossless retrieval over verbatim, finely decomposed, timestamped events, with fact and time keys, provenance, and explicit supersession or version semantics. Several 2026 preprints converge on it, but they are not yet peer-reviewed and their LoCoMo numbers are inconsistent across papers.
- Contradiction and update handling is the weakest ability in every benchmark (MemoryAgentBench FC-MH ≤28%, BEAM CR ≈0–5%, EverMemBench). It should be handled by runtime-owned, deterministic version and supersession records, not left to model judgment.

### Gaps
- Peer-review status of MemoryAgentBench, BEAM, MemGPT and LoCoMo was not re-verified in this session. LoCoMo is reported as ACL 2024 and MemGPT was later posted as a workshop paper or preprint.
- I did not retrieve the LongMemEval per-ability breakdown (e.g., knowledge-update or abstention accuracy for each model), nor the exact LongMemEval_M QA numbers from Figure 5.
- Zep's published rebuttal of Mem0's LoCoMo numbers, and Zep's own DMR claim (reported elsewhere as ~94.8%), were not fetched or verified.

---

## 2. "Lost in the middle" and "context rot": degradation with length and position, and what it implies for a handoff brief

### Takeaway
Accuracy depends on *where* the relevant fact sits (U-shaped curve: beginning and end are best, middle is worst) and drops as irrelevant tokens are added, even on trivial tasks and in frontier 2025 models. A short, focused brief (~300 tokens) beats the same information buried in ~113k tokens for every model family Chroma tested.

### Cited Findings
- Liu et al. "Lost in the Middle" (TACL 2024; venue not re-verified) [PR]. Models are best when relevant information is at the very beginning (primacy) or end (recency) of the context. "GPT-3.5-Turbo's multi-document QA performance can drop by more than 20%." In the worst case, with 20 and 30 documents, it scores below its closed-book accuracy of 56.1%. — [arXiv 2307.03172](https://arxiv.org/abs/2307.03172)
- Exact 20-document QA accuracy by position of the answer document (index 0 / 4 / 9 / 14 / 19): GPT-3.5-Turbo 75.8 / 57.2 / 53.8 / 55.4 / 63.2%. Claude-1.3 59.9 / 55.9 / 56.8 / 57.2 / 60.1%. Oracle (answer document only) for GPT-3.5-Turbo: 88.3%. — [arXiv 2307.03172](https://arxiv.org/abs/2307.03172)
- "Extended-context models are not necessarily better at using their input context": GPT-3.5-Turbo and GPT-3.5-Turbo-16K perform almost identically. GPT-4 also shows the U-shape. The Llama-2-13B base model has a 20-point gap between best and worst position. — [arXiv 2307.03172](https://arxiv.org/abs/2307.03172)
- Chroma "Context Rot" (Hong, Troynikov, Huber, July 2025) [V – vendor technical report, not peer-reviewed]:
  - It evaluates 18 LLMs, including GPT-4.1, Claude 4, Gemini 2.5 and Qwen3. "Model performance consistently degrades with increasing input length" across all experiments.
  - Even a single distractor lowers accuracy compared with the needle alone, and four distractors compound the loss.
  - Lower needle–question similarity makes the length degradation worse.
  - Haystacks with logically coherent text hurt *more* than shuffled ones.
  - On LongMemEval, a focused prompt (~300 tokens) gives significantly higher accuracy than the full prompt (~113k) for the Claude, GPT, Gemini and Qwen families. Thinking modes help but "we still see a performance gap".

  — [Chroma research](https://research.trychroma.com/context-rot)
- Chroma's hallucination and abstention findings: "Claude models consistently exhibit the lowest hallucination rates", tend to abstain under ambiguity, and as a result score lower on full LongMemEval prompts. "GPT models show the highest rates of hallucination, often generating confident but incorrect responses when distractors are present." In a repeated-words copying task, GPT-3.5-turbo refused 60.29% of trials and Qwen3-8B produced random output from about 5,000 words. — [Chroma research](https://research.trychroma.com/context-rot)
- LoCoMo provides independent evidence that longer context increases hallucination on unanswerable questions: GPT-3.5-turbo-16K adversarial F1 falls from 13.1 at 4k to 2.1 at 16k. — [arXiv 2402.17753](https://arxiv.org/abs/2402.17753)
- Retrieved-context budget depends on the reader: Llama 3.1 8B drops beyond about 3k retrieved tokens, while GPT-4o keeps improving past 20k. — [arXiv 2410.10813](https://arxiv.org/abs/2410.10813)
- The Complexity Trap authors observe that "beyond a certain point, more context becomes a liability rather than an asset", citing lost-in-the-middle. In 3 of 5 setups, the most efficient context-managed agent beat the raw full-history agent's solve rate. — [arXiv 2508.21433](https://arxiv.org/abs/2508.21433)

### Inferences (implications for a handoff brief)
- Keep the brief short and focused rather than dumping the transcript. The ~300 vs ~113k LongMemEval gap and the LongMemEval Oracle vs _S drops (−30% to −66%) both support this.
- Put the highest-priority state, such as the open task, unresolved unknown effects and constraints, at the **start**. Repeat or close with the immediate next action at the **end**. Avoid burying must-know items in the middle.
- Present memory items as structured records (JSON or fields) with timestamps, sorted chronologically. The receiving model should first extract notes before acting (LongMemEval Chain-of-Note + JSON, up to +10 points).
- Remove topical distractors, meaning near-miss but stale or irrelevant facts. Distractors cause confident hallucination, especially in GPT-family models. Superseded facts should be dropped, or explicitly marked superseded, never listed side by side unlabeled.
- Size the budget to the receiving model. Smaller models degrade sooner (Llama 8B around 3k retrieved tokens). This argues for a per-model packet budget with *rejection* of oversize packets rather than silent truncation, consistent with Continuum invariant 9.
- Abstention behaviour differs by vendor. A brief should say explicitly what is *unknown* so the model does not invent it, and evaluation should score abstention separately.

### Gaps
- I did not obtain per-model numeric scores from Chroma's LongMemEval experiment; the report presents them as charts.
- No study found tests position effects specifically on *handoff briefs or packets* for agent resumption. The inferences above extrapolate from QA settings.

---

## 3. Measured loss from summarization and compaction (recursive summaries, ACON, OpenHands condensation, Complexity Trap)

### Takeaway
LLM summarization saves tokens but measurably loses information in conversational memory: recursive-summary baselines reach about 32–39% on DMR against about 93% for lossless retrieval, and summary values or keys underperform raw text in LongMemEval and LoCoMo. For agent trajectories, simple observation masking (dropping old tool outputs) matches LLM summarization's solve rate at equal or lower cost. Summaries also lengthen trajectories by about 13–15%, likely by masking failure signals. Optimized, task-aware compression (ACON) can keep accuracy close to uncompressed while cutting peak tokens by 26–54%, whereas naive prompted compression loses accuracy.

### Cited Findings
- **Recursive summary vs lossless paged memory (MSC-DMR):** GPT-4 Turbo scores 35.3% with a recursive summary vs 93.4% with MemGPT, and GPT-4 32.1% vs 92.5%. — [arXiv 2310.08560](https://arxiv.org/abs/2310.08560) [PP]
- **LongMemEval:** replacing sessions or rounds with extracted summaries or facts "negatively impacts QA performance due to information loss". With session summaries as keys, GPT-4o top-10 QA falls to 0.252 vs 0.676 for raw-session keys. — [arXiv 2410.10813](https://arxiv.org/abs/2410.10813) [PR]
- **LoCoMo:** summary-based RAG does not help despite high retrieval recall, "likely due to loss of information during the conversion of dialogs to summaries". Incremental summaries **hallucinate** extra details and **misattribute speakers**. Best event-summary FactScore F1 is about 45.9, so roughly half of the facts are missed or wrong. — [arXiv 2402.17753](https://arxiv.org/abs/2402.17753)
- **The Complexity Trap (Lindenbauer et al., JetBrains Research + TU Munich; NeurIPS 2025 DL4C workshop, "Deep Learning for Code in the Agentic Era") [PR – workshop].** SWE-agent on SWE-bench Verified (n=500), 95% bootstrap CIs. Observation masking keeps the last M=10 observations. LLM-Summary uses an OpenHands-style prompt, summarizing N=21 turns and keeping the last M=10.

  | Model | Raw agent | Observation masking | LLM-Summary |
  |---|---|---|---|
  | Qwen3-Coder 480B | 53.4% / $1.29 | 54.8% / $0.61 (−52.7%) | 53.8% / $0.64 (−50.4%) |
  | Gemini 2.5 Flash | 32.8% / $0.41 | 35.6% / $0.18 (−56.1%) | 36.0% / $0.24 (−41.5%) |
  | Gemini 2.5 Flash (thinking) | 40.4% / $0.56 | 36.4%† / $0.24 | **31.4%†** (−22.3%) / $0.25 |
  | Qwen3-32B | 17.0% / $1.12 | 15.0% / $0.55 | 16.0% / $0.50 |
  | Qwen3-32B (thinking) | 23.0% / $0.51 | 24.6% / $0.46 | 24.8% / $0.51 |

  († = significant vs raw agent.) — [arXiv 2508.21433](https://arxiv.org/abs/2508.21433)
- **Trajectory elongation:** LLM-Summary produces longer runs. Gemini 2.5 Flash averages 52 turns vs 44 with masking (+15%) and 50 raw (+4%). Qwen3-Coder runs are 15% longer than raw and 13% longer than masking. The authors' interpretation: summaries "mask failure signals that would otherwise prompt earlier termination". Summary generation is up to 7.2% of instance cost, and those calls get little prompt-cache reuse. — [arXiv 2508.21433](https://arxiv.org/abs/2508.21433)
- A hybrid that masks first and summarizes only as a last resort gains +2.6 percentage points in solve rate over the raw agent (Qwen3-Coder 480B, Verified-50 subset). Its cost is 7% below masking alone and 11% below LLM-Summary alone. On OpenHands (Gemini 2.5 Flash, 50 instances), masking matches LLM-Summary only after its window is retuned (M=58 vs 10), because OpenHands keeps retry turns. — [arXiv 2508.21433](https://arxiv.org/abs/2508.21433)
- **OpenHands context condenser (Calvin Smith, OpenHands blog, 9 April 2025) [V].** It summarizes older events and keeps recent ones, encoding "the user's goals, the progress the agent has made, and what still has to be done", plus "critical files and failing tests". Claims: up to 2× lower per-turn API cost, linear rather than quadratic cost growth, and SWE-bench Verified 54% vs 53% for the baseline. — [OpenHands blog](https://openhands.dev/blog/openhands-context-condensensation-for-more-efficient-ai-agents)
- **ACON (Agent Context Optimization; v3; venue unconfirmed) [PP].** It improves the compressor's guideline using failure analysis of contrastive trajectories that succeeded without compression and failed with it. Results:
  - Peak tokens fall 26–54% "while improving task success compared to existing compression baselines", with "small degradation relative to full-context baseline".
  - Distilled compressors (Qwen3-14B, Qwen3-8B, Phi-4) keep over 95% of the gpt-4.1 teacher's performance.
  - Small agents improve: Qwen3-14B on AppWorld goes from 25.6% to 33.9% (+32%), and 8-objective QA EM from 0.158 to 0.23 (+46%).

  — [arXiv 2510.00615](https://arxiv.org/abs/2510.00615)
- ACON on **OfficeBench** (gpt-4.1 agent and compressor), accuracy with peak tokens in thousands:

  | Strategy | Accuracy | Peak tokens (k) |
  |---|---|---|
  | No compression | 76.84 | 7.27 |
  | FIFO history | 67.37 | 4.02 |
  | Retrieval of past steps | 65.26 | 4.33 |
  | LLMLingua | 70.53 | — |
  | Naive prompted history summary | 71.58 | 4.40 |
  | ACON (history) | 74.74 | 4.93 |
  | Naive prompted **observation** compression | **55.79** | — |

  — [arXiv 2510.00615](https://arxiv.org/abs/2510.00615)
- ACON on **8-objective QA** (EM): no compression 0.366, FIFO 0.293, retrieval 0.331, prompting 0.376, ACON 0.373. Prompted observation compression scores 0.288. Combining history and observation compression "leads to substantial performance degradation compared to applying either compression alone". The paper warns that for high-fidelity tasks "over-compression may remove subtle facts needed for final decisions". — [arXiv 2510.00615](https://arxiv.org/abs/2510.00615)
- ACON practical cost on AppWorld: no compression costs $0.331 per task with 73.24s median latency. ACON history compression costs $0.285 but takes 87.68s, and observation compression costs $0.272 and takes 101.92s. Compression trades dollars for latency. — [arXiv 2510.00615](https://arxiv.org/abs/2510.00615)

### Inferences
- In this evidence, "summarize the past" is never better than keeping recent raw state plus retrievable verbatim history. Summaries are lossy (conversation QA), can hallucinate or misattribute (LoCoMo), and in agents can hide failure signals (trajectory elongation).
- For a continuity runtime, compaction should be **deterministic and reversible where possible**: mask or collapse old tool observations but keep them addressable by ID in the ledger, and keep authoritative facts such as milestones, evidence fingerprints and unknown actions as runtime-owned structured records, not model-written prose. LLM summarization, if used at all, should be a last resort, task-specific (ACON-style guidelines), and treated as *unverified* claims in a handoff packet.
- None of these studies measures a "hallucination rate of the summary" on agent trajectories directly; the evidence is downstream (solve rate, QA accuracy).

### Gaps
- I found no study that directly quantifies the per-fact omission or hallucination rate of LLM summaries of *agent trajectories*, as opposed to downstream task success. LoCoMo's FactScore on conversation event summaries is the closest proxy.
- JetBrains' blog version of the Complexity Trap and any 2026 follow-ups were not fetched.
- Anthropic's, OpenAI's and Cursor's compaction features have no public quantitative evaluations that I located in this session.

---

## 4. Evaluation metrics in use, and what each catches

### Takeaway
The field mainly uses LLM-judge QA accuracy split by ability type, plus retrieval recall and NDCG against annotated evidence locations, plus cost metrics (tokens, peak context, p50/p95 latency, dollars per task). Update correctness and abstention are reported as separate question categories. Summary faithfulness is measured with FactScore. Very few benchmarks report hallucination rates directly.

### Cited Findings
- **Recall of planted facts.** LongMemEval reports Recall@k and NDCG@k against human-annotated evidence locations, separately from end-to-end QA, so retrieval and reading failures can be told apart. — [arXiv 2410.10813](https://arxiv.org/abs/2410.10813). Needle-in-a-haystack variants with controlled needle–question similarity and distractors appear in [Chroma](https://research.trychroma.com/context-rot). Key-value retrieval of UUIDs appears in [Liu et al.](https://arxiv.org/abs/2307.03172).
- **Update correctness.**
  - LongMemEval's knowledge-update questions: the judge counts a response as correct if it gives the updated answer, even when it also mentions the old value. — [arXiv 2410.10813](https://arxiv.org/abs/2410.10813)
  - MemoryAgentBench FactConsolidation SH and MH, where the agent must prefer the later fact. — [arXiv 2507.05257](https://arxiv.org/abs/2507.05257)
  - BEAM's Knowledge Update and Contradiction Resolution categories. — [arXiv 2510.27246](https://arxiv.org/abs/2510.27246)
- **Abstention.**
  - LongMemEval abstention questions; judge agreement there is 0.90–0.97. — [arXiv 2410.10813](https://arxiv.org/abs/2410.10813)
  - LoCoMo adversarial questions (24.9% of the set). — [arXiv 2402.17753](https://arxiv.org/abs/2402.17753)
  - BEAM abstention, where models score best. — [arXiv 2510.27246](https://arxiv.org/abs/2510.27246)
- **Faithfulness and hallucination.**
  - LoCoMo event summarization uses FactScore precision, recall and F1 plus ROUGE, and a manual error taxonomy (hallucinated details, speaker misattribution). — [arXiv 2402.17753](https://arxiv.org/abs/2402.17753)
  - Chroma reports qualitative hallucination and abstention rates by model family under distractors. — [Chroma](https://research.trychroma.com/context-rot)
  - BEAM scores answers with nugget-based rubrics. — [arXiv 2510.27246](https://arxiv.org/abs/2510.27246)
- **Token cost and latency.**
  - Mem0 reports memory tokens per conversation and p50/p95 search and total latency (full context: 9.87s p50, 17.12s p95). — [arXiv 2504.19413](https://arxiv.org/abs/2504.19413)
  - MemoryAgentBench separates memory-construction latency from query-execution latency. — [arXiv 2507.05257](https://arxiv.org/abs/2507.05257)
  - ACON reports steps, peak input tokens, "dependency" (cumulative context dependence), API dollars and wall-clock latency. — [arXiv 2510.00615](https://arxiv.org/abs/2510.00615)
  - The Complexity Trap reports dollars per instance with paired bootstrap CIs, trajectory length in turns, and summary-call cost share. — [arXiv 2508.21433](https://arxiv.org/abs/2508.21433)
- **Judges and lexical metrics.** LongMemEval's GPT-4o judge reaches more than 97% agreement with experts. — [arXiv 2410.10813](https://arxiv.org/abs/2410.10813). Mem0 reports F1, BLEU-1 and LLM-judge scores side by side, and the lexical and judge rankings differ. For example, A-Mem's original F1 and BLEU numbers differ from its re-run judge scores. — [arXiv 2504.19413](https://arxiv.org/abs/2504.19413)

### Inferences
- A Continuum memory or handoff eval should report at least these, separately:
  1. Planted-fact recall in the packet (deterministic, no judge).
  2. Update correctness, where the superseded value must be absent or marked.
  3. Abstention on unknowns, including whether the model claims an unresolved effect is done.
  4. Faithfulness: no packet claim without a source event ID.
  5. Packet tokens and peak context.
  6. Construction and query latency.
  7. Downstream resume success.

  The benchmarks above show these can diverge widely: high retrieval recall can coexist with low QA accuracy (LoCoMo summary RAG), and lower cost can coexist with lower solve rate (Gemini Flash thinking).
- Prefer deterministic scoring over LLM judges where the runtime owns ground truth, since judge agreement is lowest for abstention and preference questions.
- Report confidence intervals or multiple runs. LoCoMo results for the same system vary by about 5–30 points across papers and harnesses.

### Gaps
- No standard benchmark measures *handoff or resume* fidelity between agent sessions or models. Existing benchmarks are QA over history (LongMemEval, LoCoMo, BEAM) or single-run agent success (SWE-bench, AppWorld).
- No benchmark reports a calibrated "memory hallucination rate", meaning false memories asserted as facts, as a headline metric. The closest are LoCoMo adversarial, LongMemEval abstention and FactScore precision.

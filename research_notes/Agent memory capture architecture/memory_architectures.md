# Long-term memory architectures for LLM agents (as of Oct 2026)

Scope: how leading agent-memory systems represent, write, update and retrieve memory; measured results; failure modes. The goal is to inform a vendor-neutral continuity layer that hands a coding agent's session to another model without losing requirements, decisions or a record of what the agent did.

Source note: arxiv.org was not reachable by direct fetch from this environment. Paper numbers below come from (a) one full-text query of the Zep paper via Firecrawl, (b) search-result excerpts of the papers' own HTML or abstract pages, and (c) for a few classic papers (MemGPT, Generative Agents, HippoRAG, Reflexion, MemoryBank, MemoryOS), the papers' well-known abstract claims, cited to their arXiv abstract pages but **not re-fetched in this session**. Those are marked [abstract, not re-verified].

## How each system represents, writes, updates and retrieves memory, and what it reports

### Takeaway
Production systems split into three families. **(1) LLM-managed tiers:** MemGPT/Letta, where the agent edits its own core memory and can delegate that to a "sleep-time" agent. **(2) LLM extract-then-reconcile fact stores:** Mem0 (ADD/UPDATE/DELETE/NOOP) and A-MEM (linked notes that evolve). **(3) Provenance-preserving temporal graphs:** Zep/Graphiti, which keeps raw episodes and has bi-temporal edges. Every headline score is self-reported, LoCoMo numbers are actively disputed between vendors, and no system was evaluated on coding-agent handoff.

### Cited Findings

**MemGPT / Letta**
- MemGPT treats the context window like operating-system memory: a "main context" (system instructions, a writable core-memory/working-context block, a FIFO message queue) plus "external context" (recall storage, the searchable full message history, and archival storage, a vector store). The LLM pages data in and out through function calls it chooses itself (self-editing memory). — [MemGPT paper, arXiv 2310.08560](https://arxiv.org/abs/2310.08560) [abstract, not re-verified]
- On Deep Memory Retrieval (DMR, gpt-4-turbo), as reported in the Zep paper: MemGPT 93.4%, full-conversation 94.4%, conversation summaries 78.6%, recursive summarization 35.3%. — [Zep paper §4.2](https://arxiv.org/html/2501.13956v1)
- Letta "sleep-time compute": enabling it creates two agents. The primary agent **lacks** tools to edit core memory. A sleep-time agent holds those tools, runs asynchronously, and writes "learned context" into labeled, character-limited memory blocks that can be shared across agents. Letta's docs label the feature experimental. — [Letta blog](https://letta.com/blog/sleep-time-compute); [Letta docs](https://docs.letta.com/guides/agents/architectures/sleeptime); [Letta sleep-time agents docs](https://docs.letta.com/guides/agents/sleep-time-agents)
- The sleep-time compute paper (arXiv 2504.13171) evaluates offline precomputation on math benchmarks (stateful GSM, AIME variants). Letta reports that moving computation to idle time reduced test-time compute with no loss in accuracy. I did not retrieve the exact figures. — [Letta blog](https://letta.com/blog/sleep-time-compute)
- Letta's LoCoMo score of 83.2 appears only as a vendor claim reprinted in third-party tables, with no independent measurement. — [digitalapplied comparison](https://www.digitalapplied.com/blog/open-source-agent-memory-mem0-letta-zep-compared)

**Mem0 / Mem0g (arXiv 2504.19413, April 2025)**
- The pipeline has two phases. **Extraction:** an LLM pulls salient facts from the latest exchange, using a rolling conversation summary and recent messages as context. **Update:** each candidate fact is compared with similar stored memories, and an LLM tool call picks **ADD** (new fact), **UPDATE** (replace with richer information), **DELETE** (the fact conflicts with existing memory) or **NOOP**. — [Mem0 paper, Appendix B algorithm](https://arxiv.org/html/2504.19413v1); [alphaXiv summary](https://www.alphaxiv.org/abs/2504.19413)
- **The base Mem0 store deletes contradicted facts. Mem0g (the graph variant) marks them invalid instead.** Mem0g's pipeline is entity extractor → relations generator (triplets) → conflict detector → LLM update resolver. The resolver "marks relationships as invalid rather than deleting them" to support temporal reasoning. — [Dwarves Memo breakdown](https://memo.d.foundation/breakdown/mem0); [alphaXiv](https://www.alphaxiv.org/abs/2504.19413)
- Paper results on LoCoMo (LLM-as-judge J): Mem0 66.88 ± 0.15% using about 1,764 context tokens with 1.440 s p95 latency. Mem0g 68.44 ± 0.17% using about 3,616 tokens with 2.590 s p95. Zep, as measured by Mem0, 65.99 ± 0.16% using 3,911 tokens. — [Mem0 paper §4.2](https://arxiv.org/html/2504.19413v1); [alphaXiv](https://www.alphaxiv.org/abs/2504.19413)
- Full context (about 26,000 tokens) has p50 9.870 s and p95 17.117 s. Mem0 is reported as a 92% p95 latency reduction (1.44 s) and Mem0g as 85% (2.6 s). The paper also claims about 26% relative improvement over OpenAI memory (66.9 vs 52.9) and more than 90% token savings. — [Mem0 paper §4.3–4.4](https://arxiv.org/html/2504.19413v1); [The AI Engineer](https://theaiengineer.substack.com/p/cognee-vs-zep-vs-mem0-vs-letta)
- In Mem0's own data, **full-context had the highest accuracy**, at the cost of 17 s and 26k tokens per response. — [The AI Engineer](https://theaiengineer.substack.com/p/cognee-vs-zep-vs-mem0-vs-letta)
- The Mem0 paper claims Zep's graph consumed more than 600k tokens, duplicated knowledge across nodes, and needed lengthy asynchronous background processing before memory was usable. — [Mem0 paper §4.5](https://arxiv.org/html/2504.19413v1)
- Mem0's later vendor-published numbers (April 2026 algorithm): LoCoMo 91.6 overall (single-hop 92.3, open-domain 76.0, temporal 92.8; mean 6,956 tokens), and LongMemEval 93.4 vs Zep's 71.2. These come from Mem0's own marketing page. A third-party table instead quotes 92.5 overall from Mem0's research page, with no independent measurement. — [Mem0 Zep-vs-Mem0 page](https://mem0.ai/library/agent-memory/zep-vs-mem0-which-ai-memory-layer-should-you-choose); [digitalapplied](https://www.digitalapplied.com/blog/open-source-agent-memory-mem0-letta-zep-compared)

**Zep / Graphiti (arXiv 2501.13956, Jan 2025)**
- The graph has three tiers: an **episode subgraph** (raw messages/text/JSON, non-lossy), a **semantic entity subgraph** (entities plus fact edges) and a **community subgraph**. Episodes and the edges derived from them keep **bidirectional indices**, so every fact can be traced to its source episode for citation or quotation. — [Zep paper](https://arxiv.org/html/2501.13956v1)
- The model is **bi-temporal**. Timeline T (when events happened) uses `t_valid`/`t_invalid`. Timeline T′ (when the system ingested them) uses `t'_created`/`t'_expired`. When a new edge contradicts an older edge whose validity overlaps it in time, the older edge's `t_invalid` is set to the new edge's `t_valid`. The edge is **invalidated, not deleted**, and new information wins. — [Zep paper](https://arxiv.org/html/2501.13956v1); [Zep temporal KG explainer](https://www.getzep.com/ai-agents/temporal-knowledge-graph/)
- Writes use an LLM. Each step sees the current message plus the last n = 4 messages. Extraction uses a "reflection" pass to reduce hallucination. Entity resolution runs an LLM over embedding and full-text candidates and merges duplicates with predefined Cypher queries. — [Zep paper](https://arxiv.org/html/2501.13956v1)
- Retrieval is hybrid: cosine similarity, BM25 and breadth-first graph search, reranked with RRF, MMR, an episode-mentions reranker, a node-distance reranker or cross-encoders. Retrieval itself makes no LLM call. — [Zep paper](https://arxiv.org/html/2501.13956v1)
- DMR: Zep 94.8% vs MemGPT 93.4% (gpt-4-turbo). With gpt-4o-mini, Zep 98.2% vs full conversation 98.0%. Zep itself calls DMR inadequate because full context already scores near ceiling. — [Zep paper](https://arxiv.org/html/2501.13956v1)
- LongMemEval_s (about 115k-token contexts):

  | Model | Zep | Full context |
  |---|---|---|
  | gpt-4o-mini | 63.8%, 3.20 s | 55.4%, 31.3 s |
  | gpt-4o | 71.2%, 2.58 s | 60.2%, 28.9 s |

  Zep used about 1.6k context tokens vs 115k for full context. — [Zep paper](https://arxiv.org/html/2501.13956v1)
- **Zep was worse than full context on single-session-assistant questions**, i.e. recalling what the *assistant* said or did: 80.4% vs 94.6% on gpt-4o (−17.7%). On knowledge-update with gpt-4o-mini it was also slightly worse (74.4% vs 76.9%). — [Zep paper](https://arxiv.org/html/2501.13956v1)
- Zep's 2026 research page claims LoCoMo 94.7% (1,459/1,540) at 87/155 ms retrieval latency. This is vendor-reported. — [Zep research page](https://www.getzep.com/research/)

**The LoCoMo dispute (Zep vs Mem0)**
- Zep first claimed about 84% on LoCoMo. Mem0's replication scored Zep at 58.44% and alleged methodology errors. Zep's GitHub response gives a "corrected" 75.14% ± 0.17 over 10 runs and says Zep beats Mem0 by about 10%. Zep's blog rebuttal ("Lies, damn lies, statistics") disputes Mem0's setup. — [zep-papers issue #5](https://github.com/getzep/zep-papers/issues/5); [Zep rebuttal blog](https://blog.getzep.com/lies-damn-lies-statistics-is-mem0-really-sota-in-agent-memory/) (could not be fetched; content via [developersdigest](https://www.developersdigest.tech/blog/best-ai-agent-memory-providers-2026)); [The AI Engineer](https://theaiengineer.substack.com/p/cognee-vs-zep-vs-mem0-vs-letta)
- Mem0's own 2026 table shows Zep anywhere from 62% to 83% on LoCoMo depending on retrieval configuration ("5/2" through "30/30"). This shows how sensitive the scores are to configuration. — [Mem0 comparison page](https://mem0.ai/library/agent-memory/zep-vs-mem0-which-ai-memory-layer-should-you-choose)
- One independent compilation reports that every headline score is self-reported and that it found no independent measurements. — [digitalapplied](https://www.digitalapplied.com/blog/open-source-agent-memory-mem0-letta-zep-compared)

**A-MEM (arXiv 2502.12110, NeurIPS 2025)**
- A-MEM is a Zettelkasten-style memory. On each write, an LLM creates a note (content, keywords, tags, contextual description, embedding), creates links to related notes, and runs **memory evolution**: the new note can trigger rewriting of the attributes and context of existing linked notes. — [A-MEM paper](https://arxiv.org/html/2502.12110v8); [OpenReview](https://openreview.net/forum?id=FiM0M8gcct)
- It reports large F1 gains over the LoCoMo baseline and MemGPT on multi-hop questions (e.g. F1 3.45 vs LoCoMo 2.55 vs MemGPT 1.18 in one setting). However, its own appendix tables show it **losing** to the LoCoMo baseline on some categories with GPT-4o (e.g. adversarial-style column: 30.31 vs 45.13 F1). — [A-MEM paper](https://arxiv.org/html/2502.12110v8)
- Cost, as measured by LightMem's authors on LoCoMo with gpt-4o-mini: A-MEM 64.16% accuracy (temporal 60.44%, open-domain 31.25%), about 21.7M total tokens, 11,754 API calls and 67,084 s of runtime. That makes it the most expensive system in that table. — [LightMem repo](https://github.com/zjunlp/LightMem)

**Generative Agents (Park et al., arXiv 2304.03442)**
- The memory stream is an append-only natural-language log of observations. Retrieval scores each memory as recency (exponential decay) + importance (an LLM rates it 1–10) + relevance (embedding cosine). **Reflection** is triggered when the summed importance of recent events crosses a threshold. It synthesizes higher-level inferences that are written back into the stream and cite the memories they came from. — [Generative Agents, arXiv 2304.03442](https://arxiv.org/abs/2304.03442) [abstract, not re-verified]
- A 2026 governance paper cites Park et al. as an example of semantic drift: re-encoding raw text into a summary and then into a reflection strips nuance. — [SSGM, arXiv 2603.11768](https://arxiv.org/html/2603.11768v1)

**HippoRAG, Reflexion, Agent Workflow Memory, MemoryBank, MemoryOS, LightMem**
- **HippoRAG** (arXiv 2405.14831) indexes open knowledge-graph triples extracted by an LLM and retrieves with Personalized PageRank. Its abstract claims up to 20% gains on multi-hop QA and single-step retrieval 10–30× cheaper and 6–13× faster than iterative retrieval (IRCoT). HippoRAG 2 (arXiv 2502.14802, "From RAG to Memory") targets continual learning without parametric updates. — [HippoRAG](https://arxiv.org/abs/2405.14831) [abstract, not re-verified]; [HippoRAG 2 listed in survey repo](https://github.com/Shichun-Liu/Agent-Memory-Paper-List)
- **Reflexion** (arXiv 2303.11366) keeps verbal self-reflections on failed attempts in an episodic buffer. Its abstract reports 91% pass@1 on HumanEval vs 80% for GPT-4. — [Reflexion](https://arxiv.org/abs/2303.11366) [abstract, not re-verified]
- **Agent Workflow Memory** (ICML 2025) is procedural memory. It induces reusable workflows from past trajectories, offline or online, and reports +24.6% relative success on Mind2Web and +51.1% relative on WebArena. On WebArena that is +12.0 absolute over the BrowserGym baseline, with fewer steps, and +7.9% over human-written workflows. — [PMLR](https://proceedings.mlr.press/v267/wang25bx.html); [alphaXiv](https://www.alphaxiv.org/abs/2409.07429); [summary](https://medium.com/@techsachin/agent-workflow-memory-using-workflows-to-guide-llm-agent-generations-aad75fe2f78a)
- **MemoryBank** (arXiv 2305.10250) uses forgetting inspired by the Ebbinghaus curve, where memories decay unless reinforced. — [MemoryBank](https://arxiv.org/abs/2305.10250) [abstract, not re-verified]
- **MemoryOS** (arXiv 2506.06326) has hierarchical short-, mid- and long-term persona tiers modeled on OS paging. LightMem's measurement on LoCoMo with gpt-4o-mini: 58.25% (eval build) and 54.87% (pypi build). Temporal accuracy was only 40.19% / 36.76%. — [LightMem repo](https://github.com/zjunlp/LightMem)
- **LightMem** (ICLR 2026) has three stages: pre-compression (LLMLingua-2 or entropy based) → topic segmentation → **offline "sleep-time" consolidation** of long-term memory. On LongMemEval it reports +2.09–6.40% accuracy (GPT) and up to +7.67% (Qwen) over the strongest baseline (A-MEM). It reports 31–106× fewer tokens and 17–159× fewer API calls (GPT), and on LoCoMo +6.1–29.3% accuracy. The authors say sleep-time consolidation "mitigates information loss and inconsistency". — [LightMem arXiv](https://arxiv.org/abs/2510.18866); [v3 HTML](https://arxiv.org/html/2510.18866v3); [HF blog](https://huggingface.co/blog/xzwnlp/lightmem)
- Other 2025–26 systems catalogued by the "Memory in the Age of AI Agents" survey (arXiv 2512.13564, Dec 2025): Hindsight (2512.12818), MIRIX (2507.07957), Memory-R1 (RL-trained memory operations, 2508.19828), Mem-α (RL-learned memory construction), Nemori, ReasoningBank (2509.25140), Memp (procedural, 2508.06433), Agentic Context Engineering (2510.04618), Dynamic Cheatsheet, MemEvolve. — [survey paper list](https://github.com/Shichun-Liu/Agent-Memory-Paper-List); [survey](https://arxiv.org/abs/2512.13564)

### Inferences
- Only Zep/Graphiti structurally keeps **non-lossy raw episodes and fact→episode provenance**. Mem0 stores derived facts, and its base variant hard-deletes conflicts. For a continuity layer that must not lose requirements, the Zep-style "immutable episodes plus derived, invalidatable facts" shape fits best, and it matches Continuum's existing append-only event ledger.
- The one category where a strong memory system underperformed full context is recall of **what the assistant itself said or did**: Zep −17.7% on single-session-assistant. That is the category a coding handoff depends on most (what the agent did). Agent actions should therefore be captured deterministically from tool-call and event logs, not extracted by an LLM from chat.
- Benchmark numbers (LoCoMo, LongMemEval, DMR) measure conversational QA about personal facts. They do not measure requirement retention, decision rationale or action history across a model switch. They should not drive design choices directly.

### Gaps
- I could not fetch the exact sleep-time compute paper figures (arXiv 2504.13171) or Letta's own LoCoMo methodology.
- The full text of Zep's rebuttal blog was not retrievable (DNS failure and scrape rate limit). The specific alleged errors are summarized only through secondary sources.
- I found no independent third-party replication of Mem0's 91.6/92.5 or Zep's 94.7 LoCoMo 2026 numbers.

## Contradictions, supersession vs deletion, provenance and temporal validity

### Takeaway
Supersession (invalidate and keep) beats deletion: Zep and Mem0g invalidate with timestamps, while base Mem0 deletes. Provenance back to the raw source is first-class only in Zep/Graphiti (episode indices) and Generative Agents (reflections cite evidence). A-MEM's memory evolution actively *rewrites* older notes, which is the opposite of what an audit-grade continuity layer needs.

### Cited Findings
- Zep: bi-temporal edges (`t_valid`/`t_invalid` for event time, `t'_created`/`t'_expired` for system time). A contradiction sets `t_invalid` on the old edge. Retrieval returns the fact that was valid at query time, with provenance back to its source episode. — [Zep paper](https://arxiv.org/html/2501.13956v1); [Zep explainer](https://www.getzep.com/ai-agents/temporal-knowledge-graph/)
- Mem0: the DELETE operation removes a memory when "information conflicts with existing memory", and UPDATE replaces a memory's text in place. Mem0g instead soft-deletes (marks relationships invalid) "to preserve historical context". — [Mem0 paper algorithm](https://arxiv.org/html/2504.19413v1); [Dwarves Memo](https://memo.d.foundation/breakdown/mem0)
- Mem0's platform API also lets callers overwrite a memory's text directly (`client.update(memory_id, {text, metadata, timestamp})`). — [Mem0 docs](https://docs.mem0.ai/core-concepts/memory-operations/update)
- A-MEM: adding a new note can update the context, keywords and tags of existing notes (memory evolution), so earlier notes do not keep their original form. — [A-MEM paper](https://arxiv.org/html/2502.12110v8)
- SSGM (2026) separates *memory hallucination* (fabricated content stored as truth) from *temporal obsolescence* (a fact that was true but is now stale, citing Zep). It recommends periodically reconciling mutable memory against an **immutable episodic ledger** to preserve fidelity. — [SSGM, arXiv 2603.11768](https://arxiv.org/html/2603.11768v1)
- An ACL 2026 empirical study examines how memory **addition and deletion** policies affect the long-term performance of agents that follow past experience. — [ACL 2026 long paper](https://aclanthology.org/2026.acl-long.27.pdf) (details not extracted)

### Inferences
- For requirements and decisions, model updates as new events that supersede earlier ones, recording `superseded_by`, `valid_from`, `recorded_at` and `source_event_id`. Never mutate or delete in place. That mirrors Zep's bi-temporal edges and is consistent with Continuum invariant 3 (immutable events).
- Let new information win (Zep's policy) only when the source is authoritative, e.g. the user. A later *model* utterance should not silently supersede a user requirement.

### Gaps
- No paper measured the rate of wrong DELETE or UPDATE decisions in Mem0's update phase.
- How Zep handles contradictions that do not overlap in time, or that come from a lower-authority source, is not specified beyond "prioritizing new information".

## LLM-based vs deterministic extraction, and reported failure modes

### Takeaway
Almost every system uses an LLM on the write path: Mem0, Mem0g, Zep entity/fact extraction and resolution, A-MEM, Generative Agents importance and reflection, MemGPT/Letta self-editing, and LightMem summarization. Deterministic parts are limited to raw logs (Zep episodes, the Generative Agents stream, MemGPT recall storage), embeddings, BM25/graph retrieval and decay scoring. The reported failure modes are hallucinated memories, semantic drift from repeated summarization, temporal obsolescence, memory poisoning, graph duplication and processing lag.

### Cited Findings
- Zep's write path is LLM-based (extraction with reflection "to minimize hallucinations", LLM entity resolution). Its retrieval path is deterministic hybrid search with no LLM. — [Zep paper](https://arxiv.org/html/2501.13956v1)
- Mem0's write path is two LLM calls (extraction, then update decision) per exchange. — [alphaXiv](https://www.alphaxiv.org/abs/2504.19413)
- Recursive summarization scored only 35.3% on DMR vs 94.4% for full conversation (gpt-4-turbo), the clearest measured cost of lossy summarization. — [Zep paper](https://arxiv.org/html/2501.13956v1)
- Semantic drift: re-encoding raw text → summary → reflection strips nuance. In SSGM's worked example, "I like mild spicy food" turns into "loves very spicy food". The paper also describes procedural drift (reinforcing suboptimal workflows), goal drift, and internalizing hallucinations or malicious injections as knowledge. Its mitigation is anchoring to an immutable ledger. — [SSGM](https://arxiv.org/html/2603.11768v1)
- Practitioner guidance describes the "telephone game" effect of recursive summarization and recommends anchoring summaries to immutable snapshots. — [SitePoint 2026 guide](https://www.sitepoint.com/ai-agent-memory-guide/) (secondary)
- Graph construction cost and lag: Mem0 reports Zep's graph at more than 600k tokens, with duplicated facts and background processing delays before memory became usable. A commenter separately notes the gap between "ingestion acknowledged" and "fact retrievable" as a missing measure in comparisons. — [Mem0 paper §4.5](https://arxiv.org/html/2504.19413v1); [devgenius comments](https://blog.devgenius.io/ai-agent-memory-systems-in-2026-mem0-zep-hindsight-memvid-and-everything-in-between-compared-96e35b818da8)
- Memory-construction cost is large for LLM-heavy designs. On LoCoMo, A-MEM used about 11.5M memory-construction tokens and about 60,600 s of construction runtime, against about 2.9M tokens for MemoryOS. — [LightMem repo](https://github.com/zjunlp/LightMem)
- Letta's sleep-time agents are marked experimental and possibly unstable. — [Letta docs](https://docs.letta.com/guides/agents/sleep-time-agents)

### Inferences
- Treat any LLM-extracted memory as a *claim with provenance*, not a fact. That is consistent with Continuum invariant 8: the handoff packet must not carry unverified model claims as facts.
- Use deterministic capture for anything the runtime can observe (tool calls, file edits, commands, test results, git state). Use LLM extraction only where it adds value, such as distilling requirements or decisions from free text, and always link back to the source message span.
- Background consolidation in the style of Letta or LightMem sleep-time agents is useful, but only if it writes *derived, invalidatable* records and leaves the source log untouched.

### Gaps
- I found no quantitative study of the hallucinated-memory rate for Mem0 or Zep extraction, i.e. the percentage of stored facts not supported by the source.

## Separating episodic, semantic and procedural memory

### Takeaway
Surveys and recent systems converge on keeping raw episodic logs, derived semantic or factual memory, and procedural or experiential memory (workflows, strategies, reflections) separate. The 2025 survey frames this as "factual" vs "experiential" memory. The evidence, though limited, favors combining semantic and procedural memory over either one alone.

### Cited Findings
- The "Memory in the Age of AI Agents" survey (Dec 2025, 102 pages) organizes its paper list by function into **Factual Memory** and **Experiential Memory**, with sub-groupings by form (e.g. token-level). — [survey](https://arxiv.org/abs/2512.13564); [paper list](https://github.com/Shichun-Liu/Agent-Memory-Paper-List)
- Zep's architecture separates episodic (episode subgraph), semantic (entity/fact subgraph) and community summaries. — [Zep paper](https://arxiv.org/html/2501.13956v1)
- Procedural memory, in the form of workflows induced from trajectories (AWM), improved WebArena success by 51.1% relative. — [PMLR](https://proceedings.mlr.press/v267/wang25bx.html)
- In Exp-SWE-Agent on a SWE-Bench Pro subset, experiential-only memory (44.1% Pass) beat factual-only (43.1%), and the two combined were best (47.5% vs 42.7% baseline). — [Exp-SWE-Agent, ACM](https://dl.acm.org/doi/pdf/10.1145/3803437.3807663)

### Inferences
- For a coding handoff layer, a reasonable mapping:
  - **Episodic:** the immutable event ledger (messages, tool calls, results).
  - **Semantic:** requirements, decisions and constraints with supersession and provenance.
  - **Procedural:** repo-specific how-to, such as build and test commands, plus failed approaches to avoid.

  Continuity within one task mostly needs episodic and semantic memory. Procedural memory matters more across tasks.

### Gaps
- I found no controlled study comparing a unified memory store against separated stores on the same agent benchmark.

## Results specific to coding agents or software-engineering trajectories

### Takeaway
Evidence is thin and nearly all of it is about *cross-task experience reuse*, not within-session handoff across models. The gains are modest: a few points of resolution rate plus shorter trajectories. One practitioner benchmark found memory did not improve code quality.

### Cited Findings
- **Exp-SWE-Agent** (Peking Univ., 2026) runs a retrieve–act–distill loop that stores structured factual and experiential memory from past issue trajectories. On a sequential 8×25 SWE-Bench Pro subset, Pass% rose from 42.7% to 47.5% and average steps fell from 165.8 to 151.0. Fact-only reached 43.1% and experience-only 44.1%. Inference cost rose modestly, and the authors note non-negligible run-to-run variance. — [ACM PDF](https://dl.acm.org/doi/pdf/10.1145/3803437.3807663)
- **SWE-Exp** (arXiv 2507.23361) reports 41.6% Pass@1 on SWE-bench Verified with experience-driven issue resolution, under open-source agent frameworks. — [HF papers](https://huggingface.co/papers/2507.23361)
- **SWE-ContextBench** (Zhu et al., 2026) benchmarks experience reuse from a static pool of prior trajectories. Oracle-provided summaries improve resolution by about 8 percentage points. **ExpeRepair** (Mu et al., 2025) ablates dual episodic and semantic memory on SWE-Bench Lite and finds that both contribute. (Both reported via a secondary source.) — [Sandelin, Medium](https://medium.com/@mrsandelin/the-first-controlled-benchmark-of-ai-memory-in-coding-agents-8e0bb776d39e); also cited in [SWE-Explore related work](https://arxiv.org/html/2606.07297v1)
- A practitioner "controlled benchmark of AI memory in coding agents" reports 15–28% savings, presumably in effort or tokens. It finds memory "doesn't make coding agents write better code": all conditions hit the same 84–96% quality ceiling, and the effect grows with codebase complexity and vanishes on simple tasks. This is a self-published vendor post. — [Sandelin, Medium](https://medium.com/@mrsandelin/the-first-controlled-benchmark-of-ai-memory-in-coding-agents-8e0bb776d39e)
- Reflexion's verbal episodic memory reached 91% pass@1 on HumanEval. — [Reflexion](https://arxiv.org/abs/2303.11366) [abstract, not re-verified]

### Inferences
- No published benchmark measures what Continuum cares about: losing requirements, decisions or action history when a session moves between models. Continuum's own handoff trials and benchmark would be novel evidence. The closest public proxies are the knowledge-update and single-session-assistant categories of LongMemEval.

### Gaps
- I found no study of cross-vendor model handoff (e.g. Claude → GPT/Codex → open-source) using an external memory layer.
- I found no data on memory-system effects for multi-session coding work on the same repository, as opposed to independent issues.

# Memory and product research (October 2026)

Four research tracks ran before any code changed:
- memory techniques
- how coding apps record branches, rewinds and images
- product, market and moat
- protecting the work

This report gives the findings, what Continuum now does (D-046, D-047) and what is still open.
Sources are listed per section. Numbers marked "vendor" come from the vendor and have not been
checked independently.

## 1. Memory: what works, measured by others

| Finding | Evidence |
|---|---|
| The well-known memory systems all use an LLM to write memories (Mem0, Zep/Graphiti, Letta/MemGPT, A-MEM, MemoryOS, Memobase, Supermemory, LightMem; Claude Code, Codex and Cursor compaction) | system papers and docs |
| Plain keyword retrieval (BM25) over a verbatim store matches or beats them: overall score 41.5, against MemGPT 28.3, Zep 24.0, Mem0 21.1; best embedding retrieval 38.2; whole history in context 48.8–49.6 | MemoryAgentBench (arXiv 2507.05257, ICLR 2026) |
| Changed facts that something else depends on ("conflict resolution") score at most 7% for every method | MemoryAgentBench |
| Whole history in context drops to 0.606 accuracy, against 0.870 with only the relevant sessions. At the level of single turns, embeddings beat BM25 by about 19 recall@5 points on paraphrase | LongMemEval (arXiv 2410.10813) |
| Even models with 1M-token windows degrade as dialogues grow; episodic plus working memory helps (+3.5–12.7%) | BEAM (arXiv 2510.27246) |
| Hiding old tool outputs ("observation masking") halves cost and matches LLM summaries on SWE-bench | "The Complexity Trap" (arXiv 2508.21433) |
| Keep everything, load just in time, keep compression restorable (drop content, keep a pointer) | Anthropic context engineering; Manus |
| Splitting identifiers (camelCase, snake_case) as well as keeping them whole lifts code keyword retrieval (NDCG@10 0.309 → 0.563, single preprint) | arXiv 2605.18561 |
| Crash deduplication matches normalised stack frames, weighted by position and rarity | TraceSim (2009.12590), S3M (2103.10526) |

**Conclusion for Continuum.** The verbatim journal plus recall was already the right
architecture. Continuum's deterministic "replaced by #n" handles exactly the task every method
fails. The gaps were in capture (data loss), technical content, the chat's shape and
navigation. No benchmark measures "which user requirements must survive a coding chat", so
`memory_bench` remains the yardstick.

## 2. How the apps record branches, rewinds and images

- **Claude Code.**
  - Records form a tree (`uuid`, `parentUuid`; `logicalParentUuid` across compaction).
  - Edit-and-resend, or a rewind followed by a new message, creates a sibling user message under the same parent.
  - Parallel tool calls also fork the tree, but never with a second user message.
  - Checkpoints restore only Claude's own edits, not shell or manual ones. `/branch` and `--fork-session` write a new session file.
  - Images are `{type: image, source: {base64}}` parts; screenshots from tools sit inside `tool_result.content`.
  - No hook fires on a rewind, and `UserPromptSubmit` carries no images.
- **Codex.** A backtrack appends `thread_rolled_back {num_turns}` to the rollout. Images are `input_image` data URLs.
- **Gemini CLI.** `/rewind` and `/restore` use a shadow git repository. Images are `inlineData`.
- **Cursor.** Restoring a checkpoint reverts files only, not messages. Prompt attachments are file paths only.
- **This session's own transcript** (33 MB, 8,531 records) contained a real edit-and-resend:
  - three sibling prompts under one parent;
  - the old Continuum captured all three as requirements;
  - the new one withdraws exactly the two abandoned ones.

## 3. What changed in Continuum (D-046, D-047)

| Gap | Now |
|---|---|
| Messages over 16,000 characters were cut | whole text kept in a blob; head and tail inline |
| Images dropped; tool screenshots stored as base64 text | stored as bytes (`coding_media`), labelled in the brief, shown by `coding_view_image` |
| Pastes replaced by "[N pasted lines omitted]" | typed summaries: error signatures, diffs, JSON keys, tables, logs, shell, code |
| Abandoned branches captured as requirements | conversation tree: abandoned messages withdrawn (`ChatWithdrawn`), still searchable |
| The middle of long chats invisible except through recall | index of every stretch by its most distinctive words (tf-idf) |
| Requirements ranked by cue words and recency only | plus grounding in what the agent did, and a near-duplicate filter |
| Recall missed `getUserById` for "user by id" | identifier parts matched; reciprocal rank fusion |
| A lost `git push` always needed a person | proven by read-only `git ls-remote`; absence never inferred |

**Measured after the change.**
- Memory benchmark: 100% on every kind at 10–160 messages, 0 noise leaks.
- Pasted errors reach the brief: 100% up to 80 messages; 53% at 160 (only the 5 newest are listed).
- Brief size: about 1.4k tokens at 40 messages, 3.4k at 160.
- Real Claude Code (Trial 7, rerun): 11/11 tool calls and 3/3 changed files captured.
- `make check`: 482 tests pass, including the image, branch, rollback, recall and push-proof scenarios.

**Still open.**
- Tool calls made on an abandoned branch are not yet marked.
- Forked sessions (`--fork-session`) are captured again as a new chat.
- Cursor file attachments are not linked.
- There is no OCR; images are found by their message, not their content.
- Embeddings (for paraphrased queries) are a possible optional local model.
- Probes cover `git push` only; npm/PyPI publish and pull-request creation are natural next ones.

## 4. Product, market and moat (summary of the product track)

**Distribution.** Continuum is not a skill: a skill is instructions the model may follow, and it
cannot watch every tool call, block a push or keep a database. Continuum's value comes from:
- hooks on every call;
- a local MCP server for recall;
- a refusal the model cannot skip.

The right form is what successful tools ship:
- a local CLI and hooks;
- a plugin per app;
- a local MCP server;
- an optional cloud for team sync.

A hosted remote MCP server alone cannot see local files or hooks.

**Market.** Capture and memory are crowded and often free:
- Entire (former GitHub CEO; $60M seed, February 2026) captures the same apps' sessions into git.
- SpecStory ($25/month Pro, $300/month Team).
- claude-mem (free; about 79k GitHub stars).
- Built-in memory in Claude Code and GitHub Copilot.
- Mem0, Zep, Letta and Supermemory for general agents.
- Cursor removed its own memories in 2.1.

What is **not** crowded:
- **Proven safety:** no repeated side effect, unknown outcomes reconciled with evidence, "done" only with checks on the current code.
- **Vendor-independent evidence** in the customer's own database.

The labs are unlikely to build these: neutrality works against their lock-in, and an auditor does
not accept a vendor grading its own agent.

**Recommendation from the research (not yet decided):**
- Position Continuum as *the safety and evidence layer for coding agents*, not as "memory".
- Buyers are platform, developer-experience and security leads.
- A possible shape:
  - a free tier for individuals (distribution);
  - a team tier with a shared ledger and policies;
  - a self-hosted enterprise licence with tamper-evident export and evidence packs.
- Next differentiators, in order:
  1. more probes (publish, pull requests, deploys);
  2. per-repository policy rules enforced the same way in every app;
  3. a signed evidence pack per pull request;
  4. a team-wide "already done or unknown" check before risky actions;
  5. importing other tools' captures.
- Demand for side-effect safety is plausible but unproven: validate with about five teams before building more capture.

Sources: TechCrunch (Entire, 2026-02-10), docs.entire.io, specstory.com/pricing,
github.blog changelog 2026-01-15 (Copilot agentic memory), forum.cursor.com (memories removed),
letta.com/pricing, pricingsaas.com (Zep), Thoughtworks Radar (Agent Trace), CSA note on the EU AI
Act timeline.

## 5. Protecting the work (general notes)

- Copyright protects the code as written, not the idea. An independent re-implementation of
  the idea is not prevented by any mark in the code.
- To protect the method itself:
  - **Patents.** India: the 2025 CRI guidelines allow software with a technical effect, and a provisional application dates the claim for 12 months. US: a provisional application. Ask a patent agent before publishing details.
  - **Trade secrecy plus contracts** (the licence already requires confidentiality).
- To prove authorship and dates:
  - named, dated records (copyright registration, a signed attestation);
  - signed git tags;
  - third-party timestamps (OpenTimestamps, RFC 3161);
  - archived SHA-256 hashes of releases (in India, a Bharatiya Sakshya Adhiniyam s.63 certificate).
- The owner holds a private procedure for marking and verifying copies. It is deliberately not
  documented in this repository.

---

_Continuum is proprietary and confidential. © 2026 the Continuum copyright holder. All rights reserved. See [LICENSE](../LICENSE)._

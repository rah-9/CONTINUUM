# Model adapters and continuation contract

## 1. Boundary

Model providers are **replaceable reasoning engines**. They do not own task progress, action identity, permissions, decisions about retries or verified results.

For v1, implement **one adapter protocol**, one deterministic fake adapter and a thin LiteLLM-based production adapter tested against **three distinct provider configurations**. Do not write three separate provider SDK wrappers unless contract tests prove a provider needs one. Pin exact provider/model IDs and pricing snapshot at benchmark execution; do not hardcode current model IDs in this specification.

## 2. Typed data contract (Pydantic sketch)

```python
from typing import Any, Literal, Protocol
from pydantic import BaseModel, Field

class ToolProposal(BaseModel):
    provider_call_id: str | None = None  # correlation only, NOT operation identity
    tool_name: str
    arguments: dict[str, Any]

class Message(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str | None = None
    tool_proposals: list[ToolProposal] = Field(default_factory=list)
    reply_to_call_id: str | None = None

class Usage(BaseModel):
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: str | None = None          # Decimal encoded as text
    source: Literal["reported", "estimated", "unknown"] = "unknown"

class ModelResponse(BaseModel):
    message: Message
    usage: Usage
    finish_reason: Literal["stop", "tool_calls", "length"]

class ModelAdapter(Protocol):
    async def complete(
        self,
        *,
        messages: list[Message],
        tools: list[dict[str, Any]],
        max_output_tokens: int,
        timeout_seconds: float,
    ) -> ModelResponse: ...
```

Errors use typed categories: `rate_limited(retry_after)`, `provider_unavailable`, `timeout`, `bad_request`, `context_overflow`, `invalid_tool_output`, `unsupported_capability`, `unknown`. Never transform a malformed or missing tool result into a successful `Message`.

The **runtime**, not the provider adapter, redacts and persists the normalized response, then references it in `ModelCallFinished` for diagnostic replay.

No streaming contract in v1. Tool calls are serialized **at gateway dispatch**, even when providers return multiple proposals. The gateway validates every proposal and may reject it. Refuse model text presented as a fabricated tool-result message.

## 3. Capability profile (snapshot per model session)

```yaml
alias: A
model_id: provider/exact-version
context_window_tokens: 0        # fill with verified provider values in config
max_output_tokens: 0
supports_native_tools: true
supports_json_schema: false
supports_tool_call_ids: true
price:
  input_per_million_usd: null
  output_per_million_usd: null
  as_of: null
profile_source: provider_docs_and_contract_tests
```

Profiles must be validated during provider contract testing; placeholder zero values **must reject deployment**. A model with no usable tool-calling contract is **unsupported** in v1; no fragile “JSON in plain prose” fallback. Smaller context means deterministically reduce optional tool-result context, **never** permissions, pending effects, required evidence references or goals. If the resulting packet cannot fit, stop with `unsupported_capability`, don't silently truncate invariants.

The runtime counts usage for the aggregate task, including provider errors and retries when measurable. Provider-failed requests with unknown usage must flag uncertainty; don't claim exact budgets or prices if unreported.

## 4. Continuation packet

Packet serialization is canonical JSON, schema-versioned, and deterministic for the **same persisted snapshot, capability profile and packet-builder version**. Do not include generated timestamps or randomized ordering.

```json
{
  "packet_version": 1,
  "run_id": "run_example",
  "task": {"goal": "...", "instructions": "...", "constraints": []},
  "verified_milestones": [{"id": "m1", "evidence_ref": "...", "fingerprint": "..."}],
  "pending_milestones": [{"id": "m2", "depends_on": ["m1"]}],
  "artifacts": [{"path": "artifacts/a.csv", "sha256": "..."}],
  "confirmed_actions": [{"operation_key": "...", "result_ref": "..."}],
  "open_actions": [],
  "allowed_tools": ["csv.normalize", "store.put"],
  "remaining_budget": {"model_calls": 10},
  "data_refs": []
}
```

Rules:

1. `verified_milestones` originate **only** from valid versioned verifier evidence and unchanged dependencies; no assistant claim is promoted to verification.
2. Include **all** confirmed operation keys required to avoid repeat work and all unresolved action references. If unknown write exists, build packet only for inspection; **do not resume side-effecting execution**.
3. Artifacts are referenced by validated hashes, not copied as raw large files. Only required small verified facts appear inline.
4. The latest unverified tool results may be passed in `data_refs` as clearly labeled **untrusted data**. Never blend tool output, retrieved documents or a prior model's text into system instructions.
5. Include the tool **schemas** separately when constructing provider request. Derive allowlist from task spec and capabilities. No tool addition during handoff.
6. Packet target is a configurable fraction of confirmed model context (default <= 25%). Reserve output tokens and tool schema tokens. Reduction order: omit old nonessential untrusted data, compact evidence descriptions but **retain references**, then fail if over capacity.
7. Emit `ModelSwitched` with from/to IDs, reason, packet hash, profile snapshot reference and truncation flags. Changing model must **not** mutate actions, budgets or milestone truth.

## 5. Handoff and fallback policy

- Trigger when model request fails after bounded same-provider retry/backoff, context overflow, repeated invalid provider tool responses, or **explicit** user selection.
- Evaluate compatibility before committing the switch. Do not change models solely to circumvent a run-wide hard budget.
- On process restart acquire ownership, identify ambiguous attempts, reconcile or pause, check evidence dependencies, **then** open the new model session.
- No raw chat history migration. A packet may omit unverified conversational context; continuity is defined by **state/effect correctness**, not identical prose.

Example configuration:

```yaml
models:
  A: {model_id: "provider_a/exact-version"}
  B: {model_id: "provider_b/exact-version"}
  C: {model_id: "provider_c/exact-version"}
routing:
  primary: A
  fallbacks: [B, C]
  max_handoffs: 3
  backoff: {retries: 2, base_seconds: 1, max_seconds: 10}
```

## 6. Required adapter tests

- Text response, single/multiple valid tool proposals, correct correlation of tool response IDs.
- Rejected malformed JSON/arguments; no direct tool dispatch from adapter.
- 429/5xx/timeout/context overflow mapped to typed errors, bounded retry.
- Token/cost `unknown` stays unknown; no negative usage; measured provider configuration recorded.
- Two providers generate valid packets and resume fake-workflow milestones; a third provider configuration passes contract tests for evaluation.
- No tool support -> explicit incompatibility; small context -> capacity error without dropping essential references.

# Switching models: versions, vendors and chats

A run's progress belongs to Continuum, not to any model. Verified milestones, confirmed effects,
budgets and pending decisions all live in PostgreSQL. A new model always starts from a
deterministic continuation packet built from that state. It never starts from another model's
transcript. That is why switching models, whether to a new version, another vendor or another
chat, does not lose work and never repeats an effect.

## Three kinds of switch

| You change… | Example | What happens to Continuum runs |
|---|---|---|
| **Your coding agent's model** | Codex GPT-5.6 → GPT-6.1; Claude Code Opus 4.8 → Opus 5.5 | Nothing changes. The MCP tools are the same for every client model. Any chat can `list_runs`, `get_status` and `resume_run` any run. |
| **The model Continuum runs a task with** | resume a run on another model | `resume_run(run_id, model_alias=…)` or `continuum resume <run> --model …`. The new model receives the packet with `handoff` provenance and continues the first unverified milestone. |
| **A model's version in config** | alias `A`: `openai/gpt-5.6` → `openai/gpt-6.1` | The next resume or start uses the new version. Old sessions keep their recorded model id and profile for audit. |

To keep **coding work** across a switch of your coding agent's model (what was decided, what is proven, where the code is), use coding continuity: `coding_resume` at the start of every chat. See [CODING-HANDOFF.md](CODING-HANDOFF.md); it is measured with real models in [HANDOFF-TRIALS.md](HANDOFF-TRIALS.md).

## The "you changed the model" notice in coding apps

Some apps warn or start a new chat when you change model mid-conversation. Others keep the
chat and send its whole history to the new model. Continuum cannot change an app's own
dialog, but it makes both outcomes safe:

- **A new chat:**
  - In Claude Code the brief is put into it automatically (session-start hook, also after `/clear` and compaction).
  - In other apps the rules make the model call `coding_resume` first.
- **Keeping the chat** works too, but it is the expensive path. Prompt caches do not carry across models, and in a measured 30-message chat every call carried ~135k tokens of history. The brief carried the same requirements in ~2.1k tokens.
- **Before a deliberate switch,** run `/continuum:handoff` in Claude Code. It records decisions, a checkpoint and the next step.

Details: [MEMORY-QUALITY.md](MEMORY-QUALITY.md) §6.

## How to switch

```bash
# Continue a run on a newly released model, by id (no config edit needed)
continuum resume <run_id> --model anthropic/claude-opus-5-5

# Or upgrade an alias in config/models.yaml, then resume normally
#   A: {model_id: "openai/gpt-6.1", profile: auto}
continuum resume <run_id>
```

From an agent (MCP): `resume_run(run_id="…", model_alias="openai/gpt-6.1")`.

## What makes it safe

- **Provider-neutral history.** Tool names are mapped to provider-safe names (`store.put` ↔ `store_put`), tool-call ids are paired correctly, and each provider's request rules are met. Contract tests validate requests against OpenAI and Anthropic rules (`tests/contract/test_model_switching.py`).
- **Model-family parameters.** LiteLLM `drop_params` drops or translates parameters a model family does not accept, such as `max_tokens` for reasoning models.
- **Capability-checked profiles.** A new version needs a context window, an output limit and native tool calling. These come from a verified `config/models.yaml` entry or from the model registry, and an incomplete registry entry is refused with guidance. Unknown prices stay unknown, never zero.
- **Capability-aware fallback.** After a context overflow, models with a larger window are tried first. Incompatible models are skipped explicitly.
- **Budgets carry over.** Switching models never resets the run's model-call, token, cost or time budgets.
- **No unsafe repeats.** Confirmed operations are deduplicated by their operation key, whichever model proposes them. Unresolved effects block every model until they are reconciled or decided by a human.

## Verified scenarios (automated tests)

- OpenAI → Anthropic mid-run after an outage: one upload, no provider rejections.
- GPT-5.6 in chat 1 → crash → alias upgraded → GPT-6.1 in chat 2: verified work is not redone.
- Claude Opus 4.8 → Opus 5.5 by model id at resume: one upload, one message, and cost estimated from the new price snapshot.
- Three vendors in one run (GPT-6.1 down → Claude Opus 5.5 down → GPT-5.6): each record updated exactly once.
- Two separate MCP chats (separate server processes): the second finds the paused run, the human decides, and it resumes on another model to completion.

## Limits

Live provider behaviour is covered only by the opt-in tests (`CONTINUUM_LIVE=1`). The emulators
enforce documented request rules. They cannot prove a specific new model's reasoning quality.

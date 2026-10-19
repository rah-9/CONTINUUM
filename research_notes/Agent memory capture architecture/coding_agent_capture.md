# Coding-agent capture surfaces: what each app exposes for recording a session's full activity (as of 2026-10-19)

Scope: what a third-party continuity layer can read from each coding-agent app, without the model's help, to record inputs, outputs, tool calls and results, MCP, skills, subagents, file edits and commands, plus how each app manages its own memory and context. Sources are official docs (fetched 2026-10-19), source code on `main` of the GitHub repos (fetched 2026-10-19), and, for the Claude Code transcript, the structure of real local transcripts written by Claude Code v2.1.299–2.1.303 in this environment. The transcript check read keys and record types only, never content.

Abbreviations: **CC** = Claude Code, **CX** = OpenAI Codex CLI, **GC** = Gemini CLI.

---

## 1. Claude Code: hook events and their JSON payloads

### Takeaway
Claude Code has the richest capture surface of any app checked. More than 30 hook events, delivered by stdin JSON, HTTP POST or MCP-tool handlers, cover prompts, every tool call before and after (with the full `tool_input` and the structured `tool_response`), failures, parallel batches, subagents, compaction (including the generated summary), instruction/memory loads, model switches and session end. Hooks fire inside subagents too and carry `agent_id`/`agent_type`, so one capture path covers the main thread and all sidechains.

### Cited Findings
**Handler types and delivery**
- There are five handler types: `command` (JSON on stdin; exit code plus stdout), `http` (JSON as the POST body), `mcp_tool` (calls a tool on a configured MCP server), `prompt` and `agent`. "All matching hooks run in parallel." — [CC hooks reference](https://code.claude.com/docs/en/hooks)
- Hooks from settings, managed policy and plugins also run inside subagents. Tool events there carry the `agent_id` and `agent_type` common fields. — [CC hooks reference](https://code.claude.com/docs/en/hooks)

**Full event list** (from the hook-lifecycle table): `SessionStart`, `Setup`, `UserPromptSubmit`, `UserPromptExpansion`, `PreToolUse`, `PermissionRequest`, `PermissionDenied`, `PostToolUse`, `PostToolUseFailure`, `PostToolBatch`, `Notification`, `MessageDisplay`, `SubagentStart`, `SubagentStop`, `TaskCreated`, `TaskCompleted`, `Stop`, `StopFailure`, `TeammateIdle`, `InstructionsLoaded`, `ConfigChange`, `CwdChanged`, `DirectoryAdded`, `FileChanged`, `WorktreeCreate`, `WorktreeRemove`, `PreCompact`, `PostCompact`, `PreModelSwitch`, `PostModelSwitch`, `Elicitation`, `ElicitationResult`, `SessionEnd`. — [CC hooks reference](https://code.claude.com/docs/en/hooks)

**Common input fields**
- `session_id`, `prompt_id`, `transcript_path`, `cwd`, `scratchpad_dir`, `permission_mode`, `effort` (`{level}`), `hook_event_name`; plus `agent_id` and `agent_type` inside a subagent or under `--agent`. — [CC hooks reference](https://code.claude.com/docs/en/hooks)
  - `prompt_id` (a UUID) matches the `prompt.id` attribute on OpenTelemetry (OTel) events, which lets you correlate hooks with telemetry.
  - `scratchpad_dir` requires v2.1.257 or later.
- `transcript_path`: "The transcript file is written asynchronously and may lag the in-memory conversation." Use `last_assistant_message` on Stop and SubagentStop instead of reading the transcript at that moment. — [CC hooks reference](https://code.claude.com/docs/en/hooks)
- Only `SessionStart` can carry `model`. `PreModelSwitch` and `PostModelSwitch` carry `from_model` and `to_model`. There is no `$CLAUDE_MODEL` environment variable. — [CC hooks reference](https://code.claude.com/docs/en/hooks)

**Per-event fields**
- **SessionStart**: `source` (`startup|resume|clear|compact|fork`), optional `model`, `agent_type`, `session_title`.
  - On resume or fork (v2.1.251 or later), it also carries `seconds_since_last_response`, `context_tokens`, `prompt_cache_likely_expired` and `estimated_cache_write_usd`.
  - Source: [CC hooks reference](https://code.claude.com/docs/en/hooks)
- **InstructionsLoaded**: `file_path`, `memory_type` (`User|Project|Local|Managed`), `load_reason` (`session_start|nested_traversal|path_glob_match|include|compact`), `globs`, `trigger_file_path`, `parent_file_path`. — [CC hooks reference](https://code.claude.com/docs/en/hooks)
- **UserPromptSubmit**: `prompt`, with pasted placeholders expanded, plus `session_title`. It also fires on turns Claude Code starts on its own. `UserPromptExpansion` fires when a slash command expands into a prompt. — [CC hooks reference](https://code.claude.com/docs/en/hooks)
- **PreToolUse**: `tool_name`, `tool_input`, `tool_use_id`.
  - MCP tools also carry `mcp_server` `{name, source}` (v2.1.274 or later); the docs say to base trust decisions on `source`.
  - File-tool `file_path` arrives absolute, with `~` and relative paths already expanded.
  - Source: [CC hooks reference](https://code.claude.com/docs/en/hooks)
- **Tool input schemas documented**:
  - Bash: `command, description, timeout, run_in_background`
  - Write: `file_path, content`
  - Edit: `file_path, old_string, new_string, replace_all`
  - Agent: `prompt, description, subagent_type, model`
  - Read, Glob, Grep, WebFetch, WebSearch, AskUserQuestion, ExitPlanMode are also documented.
  - Source: [CC hooks reference](https://code.claude.com/docs/en/hooks)
- **PostToolUse**: `tool_input` plus `tool_response` (the tool's structured `Output` object, for example Write → `{filePath, type:"create"}`), `tool_use_id`, `duration_ms`. Tool calls that fail go to `PostToolUseFailure`. — [CC hooks reference](https://code.claude.com/docs/en/hooks)
- **Bash file-change diff**: `tool_response.bashEditDiff` holds `changedFiles` (at most 200 absolute paths), `files` (diffs of up to 5 files, with `hunks` and `created`/`deleted`) and `moreFiles`.
  - It is recorded in auto mode and bypassPermissions mode, or in any mode when the `bashEditDiffEnabled` setting is on.
  - Requires v2.1.269 or later. The docs call it "best effort and in public beta", and it excludes git-ignored files and submodules.
  - Source: [CC hooks reference](https://code.claude.com/docs/en/hooks)
- **Agent (subagent) PostToolUse `tool_response`**:
  - Fields: `status` (`completed|async_launched`), `agentId`, `content`, `resolvedModel`, `modelsUsed`, `totalTokens`, `totalDurationMs`, `totalToolUseCount`, `usage`.
  - Background launches return `async_launched` with `agentId, description, prompt, outputFile, resolvedModel` and no usage.
  - Under SubagentHandback (v2.1.271 or later), the report is that tool's `tool_input.message`, not `content`.
  - Source: [CC hooks reference](https://code.claude.com/docs/en/hooks)
- **PostToolUseFailure**: `tool_name`, `tool_input`, `tool_use_id`, `error` (string whose first line is `Exit code N`; the docs say it is not a stable format), `is_interrupt`, `duration_ms`.
  - It does not fire for validation rejections, which are `tool_use_error` results and fire no hooks.
  - It does not fire for permission denials (`PermissionDenied` covers auto-mode denials) or for user cancellation.
  - Source: [CC hooks reference](https://code.claude.com/docs/en/hooks)
- **PostToolBatch**: fires once per parallel batch with `tool_calls[]`, each holding `tool_use_id` and `tool_response`. Here `tool_response` is the serialized `tool_result` content the model sees (for example, line-numbered Read text), not the structured Output. — [CC hooks reference](https://code.claude.com/docs/en/hooks)
- **PermissionRequest**: `tool_name`, `tool_input`, `permission_suggestions`, but no `tool_use_id`. **PermissionDenied**: `tool_use_id`, `reason`. — [CC hooks reference](https://code.claude.com/docs/en/hooks)
- **SubagentStart**: `agent_id`, `agent_type`.
- **SubagentStop**: `stop_hook_active`, `agent_id`, `agent_type`, `agent_transcript_path` (`.../<session>/subagents/agent-<id>.jsonl`), `last_assistant_message`.
  - It also fires for internal agents (prompt suggestions, `/btw`); for those, `agent_type` is empty or the session's agent.
  - Source: [CC hooks reference](https://code.claude.com/docs/en/hooks)
- **Stop**: `stop_hook_active`, `last_assistant_message`, `background_tasks[]` (`id, type, status, description, command, agent_type, server, tool, name`), `session_crons[]`. **StopFailure**: `error` (for example `rate_limit`), `error_details`, `last_assistant_message`. — [CC hooks reference](https://code.claude.com/docs/en/hooks)
- **PreCompact**: `trigger` (`manual|auto`), `custom_instructions`; it can block. **PostCompact**: `trigger`, `compact_summary` (the generated summary text); no decision control. — [CC hooks reference](https://code.claude.com/docs/en/hooks)
- **SessionEnd**: `reason` (`clear|resume|logout|prompt_input_exit|other`).
  - The default timeout is 1.5 s; raise it with a per-hook `timeout` (up to 60 s) or `CLAUDE_CODE_SESSIONEND_HOOKS_TIMEOUT_MS`.
  - Source: [CC hooks reference](https://code.claude.com/docs/en/hooks)
- **Notification**: `message`, `title`, `notification_type`. **Elicitation**: `mcp_server_name`, `message`, `mode` (`form|url`), `requested_schema`/`url`. — [CC hooks reference](https://code.claude.com/docs/en/hooks)

**MCP tool naming**
- MCP tools are named `mcp__<server>__<tool>`. Plugin-bundled servers use `mcp__plugin_<plugin-name>_<server-name>__<tool>`. A matcher needs `.*` to match a server prefix. — [CC hooks reference](https://code.claude.com/docs/en/hooks)

### Inferences
- For a "record everything" continuity layer, the minimal CC hook set is: PreToolUse (intent and arguments before any side effect), PostToolUse and PostToolUseFailure (result and error), PostToolBatch (the exact text the model saw), UserPromptSubmit, Stop/StopFailure, SubagentStart/Stop, PreCompact/PostCompact, InstructionsLoaded, SessionStart/SessionEnd.
- `tool_use_id` joins PreToolUse to PostToolUse/Failure and to OTel `tool_result`. `prompt_id` joins a whole turn.
- A tool call that receives PreToolUse but neither PostToolUse nor PostToolUseFailure (crash, kill, cancel) is the "unknown outcome" case. Cancellation explicitly fires no failure hook, so the layer must reconcile from the transcript or the filesystem.
- SessionEnd's 1.5 s default budget is too short for heavy persistence. Hooks should append to a local queue and return.

### Gaps
- The docs have no per-tool `tool_response` schema for every tool; only Write, Agent, ExitPlanMode and Bash `bashEditDiff` are documented. The transcript observations in §2 fill part of this.
- I did not verify whether `UserPromptSubmit` carries image or attachment content.

---

## 2. Claude Code: transcript JSONL, sidechains, edits, compaction, memory, OpenTelemetry

### Takeaway
The transcript (`~/.claude/projects/<project>/<session>.jsonl`) is the most complete after-the-fact record. It holds every message, tool_use and tool_result, plus a `toolUseResult` sidecar.
- Edit and Write results include `originalFile` and `structuredPatch`, so before and after content can be rebuilt.
- Subagents get their own JSONL under `<session>/subagents/`.
- Compaction leaves a `compact_boundary` system record and an `isCompactSummary` user message.

The transcript format is undocumented beyond its purpose and changes between versions. OTel adds correlated, content-gated events: `tool_result` has no output by default, and full tool output needs the beta tracing flags.

### Cited Findings
**Files on disk**
- `projects/<project>/<session>.jsonl` is the "Full conversation transcript: every message, tool call, and tool result".
- `projects/<project>/<session>/subagents/` holds subagent transcripts.
- `projects/<project>/<session>/tool-results/` holds "Large tool outputs spilled to separate files".
- `file-history/<session>/` holds "Pre-edit snapshots of files Claude changed, used for checkpoint restore", keeping the 100 most recent checkpoints.
- `history.jsonl` holds every typed prompt.
- Source: [CC .claude directory](https://code.claude.com/docs/en/claude-directory)
- Transcripts are plaintext and not encrypted. "If a tool reads a `.env` file or a command prints a credential, that value is written to" the transcript. Transcripts are deleted after `cleanupPeriodDays`. — [CC .claude directory](https://code.claude.com/docs/en/claude-directory)

**Observed record schema** (local transcripts, v2.1.299–2.1.303, 45 files; field names verified, content not read)
- Top-level `type` values: `user`, `assistant`, `system`, `attachment`, `last-prompt`, `mode`, `queue-operation`, `cost-state`, `api-request`, `api-request-blob`, `api-request-shape`, `atis-latch`. `system` subtypes seen: `stop_hook_summary`, `compact_boundary`.
- Message records carry:
  - identity: `uuid`, `parentUuid`, `sessionId`, `timestamp`, `version`
  - location: `cwd`, `gitBranch`, `entrypoint`
  - agent and sidechain: `isSidechain`, `agentId`
  - prompt link: `promptId`
  - tool-result link: `sourceToolUseID`, `sourceToolAssistantUUID`
  - permissions: `permissionMode`, `permissionDecision`
  - compaction: `isCompactSummary`
  - the result sidecar: `toolUseResult`
- Assistant records add `requestId`, `requestedModel`, `effort`, and attribution fields (`attributionSkill`, `attributionMcpServer`, `attributionMcpTool`, `attributionAgent`).
- Content blocks: assistant `text`, `thinking`, `tool_use`; user `tool_result` and `text`.
- `toolUseResult` key sets seen:
  - **Edit**: `filePath, oldString, newString, originalFile, replaceAll, structuredPatch, userModified`, optional `gitDiff`.
  - **Write**: `type, filePath, content, originalFile, structuredPatch, userModified`, optional `gitDiff`.
  - **Bash**: `stdout, stderr, interrupted, isImage, noOutputExpected`, optional `returnCodeInterpretation, backgroundTaskId, timedOutAfterMs, gitOperation, bashEditDiff`.
  - **Agent**: `agentId, agentType, status, prompt, content, resolvedModel, totalDurationMs, toolStats, handback, handbackReport`; async launches carry `isAsync, outputFile`.
  - **Glob/Grep**: `filenames, numFiles, mode, numLines`.
- `structuredPatch` hunks are `{oldStart, oldLines, newStart, newLines, lines[]}`.
- `attachment` subtypes include `edited_text_file`, `skill_listing`, `hook_success`, `instructions`, `mcp_instructions_delta`, `deferred_tools_delta`, `compact_file_reference`, `queued_command`.
- `compact_boundary.compactMetadata` keys: `trigger, preTokens, postTokens, durationMs, cumulativeDroppedTokens, preCompactTaskList, preCompactDiscoveredTools, runningTurnSends`.
- `cost-state` carries `totalCostUSD, totalLinesAdded, totalLinesRemoved, modelUsage, totalToolDuration`.
- Source for all of the above: local `~/.claude/projects/*/*.jsonl` inspection, 2026-10-19 (primary observation, not official docs).

**Memory and context**
- CLAUDE.md files (Managed, User, Project, Local, plus `.claude/rules/*.md` and AGENTS.md) load every session. The docs recommend keeping each under 200 lines; Claude Code loads files up to 4 MiB in full.
- "Auto memory" is notes Claude writes itself, stored in `~/.claude/projects/<project>/memory/`: a `MEMORY.md` index plus topic files with a `type` frontmatter field.
  - The first 200 lines or 25KB of `MEMORY.md` load at session start.
  - The directory is shared by worktrees of the same repo, is machine-local and is excluded from transcript cleanup.
  - Disable it with `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` or `autoMemoryEnabled`.
- Source: [CC memory](https://code.claude.com/docs/en/memory)
- Subagents can keep their own auto memory. — [CC memory](https://code.claude.com/docs/en/memory)
- Compaction:
  - It triggers manually (`/compact`) or automatically at the auto-compact window, and also as "idle compaction".
  - Instruction files reload after compaction (`InstructionsLoaded.load_reason="compact"`).
  - SessionStart fires with `source:"compact"`.
  - Source: [CC hooks reference](https://code.claude.com/docs/en/hooks)

**OpenTelemetry**
- Enable with `CLAUDE_CODE_ENABLE_TELEMETRY=1` plus `OTEL_METRICS_EXPORTER`/`OTEL_LOGS_EXPORTER` (`otlp|console|prometheus`). — [CC monitoring](https://code.claude.com/docs/en/monitoring-usage)
- Content gates, all off by default:
  - `OTEL_LOG_USER_PROMPTS`
  - `OTEL_LOG_ASSISTANT_RESPONSES`
  - `OTEL_LOG_TOOL_DETAILS`: tool parameters and input; MCP server and tool names; skill names
  - `OTEL_LOG_TOOL_CONTENT`: tool output, in the `tool.output` span event; requires tracing
  - `OTEL_LOG_RAW_API_BODIES`: full Messages API request and response bodies; `=1` sends them inline (truncated), `file:<dir>` writes untruncated files plus an `index.jsonl`
  - Content limit `CLAUDE_CODE_OTEL_CONTENT_MAX_LENGTH`, default 61440
  - Source: [CC monitoring](https://code.claude.com/docs/en/monitoring-usage)
- Event names: `claude_code.user_prompt`, `assistant_response`, `tool_result`, `api_request`, `api_error`, `api_refusal`, `api_request_body`, `api_response_body`, `tool_decision`, `permission_mode_changed`, `mcp_server_connection`, `skill_activated`, `hook_execution_start/complete`, `compaction`, `subagent_completed`, and others. — [CC monitoring](https://code.claude.com/docs/en/monitoring-usage)
- `claude_code.tool_result` attributes:
  - `tool_name`, `tool_use_id` (matches hooks), `success`, `duration_ms`, `decision_source`, `tool_input_size_bytes`, `mcp_server_scope`
  - with TOOL_DETAILS on: `error`, `tool_parameters` (bash command; `mcp_server_name`/`mcp_tool_name`; `skill_name`; `subagent_type`), `tool_input` (values over 512 characters truncated, about 4K total), and `vcs.ref.head.*` for git commits
  - **No tool output on this event**; output appears only in the `tool.output` span event under `OTEL_LOG_TOOL_CONTENT=1`. Edit and Write output also needs TOOL_DETAILS, and failed calls record nothing.
  - Source: [CC monitoring](https://code.claude.com/docs/en/monitoring-usage)
- Tracing (beta) needs `CLAUDE_CODE_ENHANCED_TELEMETRY_BETA=1` and `OTEL_TRACES_EXPORTER`. It produces a `claude_code.interaction` root span per prompt; tool spans nest a permission-wait child and an execution child; subagent spans nest under the parent. Bash subprocesses inherit `TRACEPARENT`. — [CC monitoring](https://code.claude.com/docs/en/monitoring-usage)
- `skill_activated`: `skill.name` (shows `custom_skill` unless TOOL_DETAILS is on), `invocation_trigger` (`user-slash|claude-proactive|nested-skill`), `skill.source`. `compaction`: `trigger, success, duration_ms, pre_tokens, post_tokens, error`. `subagent_completed`: `agent_type, is_async, total_tokens, total_tool_uses, duration_ms, model, final_model`. — [CC monitoring](https://code.claude.com/docs/en/monitoring-usage)
- Claude Code removes `OTEL_*` exporter variables from every subprocess it spawns, including hooks. — [CC hooks reference](https://code.claude.com/docs/en/hooks)

### Inferences
- Full before and after file content per edit is available from the transcript (`originalFile` plus `structuredPatch`, or plus `content` for Write) and from `file-history/` snapshots, but not from hook payloads. A PostToolUse hook gets the structured Output object, which per the observed transcript sidecar likely contains the same keys. The docs do not confirm that.
- Bash side effects outside git, or in other modes, are only partly visible. A continuity layer should snapshot (`git status`/hash) in PreToolUse and PostToolUse itself rather than rely on `bashEditDiff`.
- Skills show up as the `Skill` tool in hooks (`tool_name:"Skill"`) and as `skill_activated` in OTel. The `skill_name` parameter appears under TOOL_DETAILS. This is inferred from the OTel `tool_parameters` doc; I did not see a Skill hook example.
- Because the transcript is undocumented and versioned, parsing should key on `type` and tolerate unknown fields and record types.

### Gaps
- There is no official spec of the transcript JSONL schema. The field list above comes from observation of v2.1.299–303 only.
- I found no documented "memory tool" for Claude Code; auto memory uses ordinary file tools on the memory directory. The API-level memory tool (`memory_20250818`) is a separate Anthropic API feature and was not researched here.

---

## 3. OpenAI Codex CLI: rollout JSONL, hooks, notify, OpenTelemetry, AGENTS.md

### Takeaway
Codex now has a Claude-Code-compatible hook system:
- events `SessionStart`, `SessionEnd`, `UserPromptSubmit`, `PreToolUse`, `PermissionRequest`, `PostToolUse`, `PreCompact`, `PostCompact`, `SubagentStart`, `SubagentStop`, `Stop`, `Interrupt`
- same field names: `tool_name`, `tool_input`, `tool_response`, `tool_use_id`
- MCP names `mcp__server__tool`

It also writes a detailed rollout JSONL whose events include command output, exit codes, per-file patch changes and a per-turn unified diff. Hosted tools such as WebSearch bypass hooks. The legacy `notify` gives only turn-completion data.

### Cited Findings
**Hooks**
- Config lives in `hooks.json` or `[hooks]` in `config.toml`, in `~/.codex/` or `<repo>/.codex/`, or in plugins. Non-managed hooks must be reviewed and trusted by hash via `/hooks`. Handler types are `command` and `mcp_tool`; `prompt`/`agent` handlers are parsed but skipped. Hooks are on by default (`[features] hooks`). — [Codex hooks](https://developers.openai.com/codex/hooks)
- Common input fields: `session_id` (subagent hooks use the parent's), `transcript_path`, `cwd`, `hook_event_name`, `model`. Turn-scoped events add `turn_id`; most add `permission_mode`. "The transcript format isn't a stable interface for hooks." — [Codex hooks](https://developers.openai.com/codex/hooks)
- **PreToolUse / PostToolUse**: `tool_name`, `tool_use_id`, `tool_input` (Bash and apply_patch use `tool_input.command`), `tool_response` (MCP call result; other local tools send their model-facing output).
  - PostToolUse runs even after Bash exits non-zero.
  - Coverage: shell, unified exec (`exec_command`), `apply_patch` (matchable as `Edit|Write`), MCP, other local function tools (`spawn_agent` matches `Agent`).
  - **Not covered**: hosted tools such as WebSearch.
  - "Treat tool hooks as a useful guardrail, not a complete enforcement boundary."
  - Source: [Codex hooks](https://developers.openai.com/codex/hooks)
- **SubagentStop**: `agent_id, agent_type, agent_transcript_path, stop_hook_active, last_assistant_message`.
- **Stop**: `stop_hook_active, last_assistant_message`.
- **PreCompact/PostCompact**: `trigger` (`manual|auto`).
- **SessionStart** `source`: `startup|resume|clear|compact|fork`. **SessionEnd** `reason`: always `other`.
- **Interrupt**: `turn_id`.
- Source: [Codex hooks](https://developers.openai.com/codex/hooks)
- Background (`async`) hooks run at most 8 at a time per session, and unfinished ones are cancelled at session end. Generated wire schemas are at `codex-rs/hooks/schema/generated`. — [Codex hooks](https://developers.openai.com/codex/hooks)

**Legacy notify**
- The JSON is appended as the last argv argument: `{"type":"agent-turn-complete","thread-id","turn-id","cwd","client","input-messages":[...],"last-assistant-message"}`. It is now produced from the AfterAgent hook event. — [codex-rs/hooks/src/legacy_notify.rs](https://github.com/openai/codex/blob/main/codex-rs/hooks/src/legacy_notify.rs)

**Rollout files**
- Layout: `~/.codex/sessions/YYYY/MM/DD/rollout-YYYY-MM-DDThh-mm-ss-<uuid>.jsonl`, with `archived_sessions/` alongside. — [codex-rs/rollout/src/list.rs](https://github.com/openai/codex/blob/main/codex-rs/rollout/src/list.rs); [rollout/src/lib.rs](https://github.com/openai/codex/blob/main/codex-rs/rollout/src/lib.rs)
- Each line is a `RolloutLine {timestamp, ordinal?, #[serde(flatten)] item: RolloutItem}`. — [codex-rs/history/src/lib.rs](https://github.com/openai/codex/blob/main/codex-rs/history/src/lib.rs)
- `RolloutItem` variants: `SessionMeta`, `ResponseItem`, `InterAgentCommunication`, `InterAgentCommunicationMetadata`, `Compacted`, `TurnContext`, `TokenUsageRecord`, `WorldState`, `SecurityRiskScore`, `RetainedContext`, `EventMsg`, `RealtimeItem`. — [codex-rs/history/src/lib.rs](https://github.com/openai/codex/blob/main/codex-rs/history/src/lib.rs)
- `SessionMeta` fields include `session_id, id, forked_from_id, parent_thread_id, timestamp, cwd, originator, cli_version, source, agent_nickname, agent_role, agent_path, model_provider`. — [codex-rs/protocol/src/protocol.rs](https://github.com/openai/codex/blob/main/codex-rs/protocol/src/protocol.rs)
- `ResponseItem` is serde-tagged `type`, snake_case.
  - Variants: `message`, `agent_message`, `reasoning`, `local_shell_call`, `function_call` (`name, namespace, arguments, call_id`), `function_call_output` (`call_id, output`), `custom_tool_call` (`call_id, name, input`), `custom_tool_call_output`, `tool_search_call/output`, `web_search_call`, `image_generation_call`, `compaction`, `context_compaction`, `compaction_trigger`, `configuration_update`, `additional_tools`, `other`.
  - Source: [codex-rs/protocol/src/models.rs](https://github.com/openai/codex/blob/main/codex-rs/protocol/src/models.rs)
- `EventMsg` (serde tag `type`, snake_case) has about 90 variants, including:
  - `user_message`, `agent_message`, `agent_reasoning`
  - `exec_command_begin/end`, `patch_apply_begin/updated/end`, `turn_diff`
  - `mcp_tool_call_begin/end`, `web_search_end`, `context_compacted`, `token_count`
  - `turn_started/complete/aborted`
  - `collab_agent_spawn_*`, `sub_agent_activity`
  - `hook_started/completed`, and `pre_tool_use`/`post_tool_use`/`session_start`/`session_end`
  - Source: [protocol.rs](https://github.com/openai/codex/blob/main/codex-rs/protocol/src/protocol.rs)
- Key event fields:
  - `ExecCommandEndEvent`: `call_id, turn_id, command[], cwd, parsed_cmd, source, aggregated_output, exit_code, duration, status, completed_at_ms`.
  - `PatchApplyEndEvent`: `call_id, turn_id, stdout, stderr, success, changes: map<path, FileChange>, status`.
  - `FileChange`: `Add{content}`, `Delete{content}`, or `Update{unified_diff, move_path}`.
  - `TurnDiffEvent`: `{unified_diff}`.
  - Source: [protocol.rs](https://github.com/openai/codex/blob/main/codex-rs/protocol/src/protocol.rs)
- Persistence policy:
  - Always persisted: `UserMessage, AgentMessage, AgentReasoning, PatchApplyEnd, ContextCompacted, McpToolCallEnd, WebSearchEnd`, token and turn events.
  - In a separate, history-mode-dependent arm: `ExecCommandEnd`, collab ends and `Error`.
  - Not persisted: streaming deltas.
  - Source: [codex-rs/rollout/src/policy.rs](https://github.com/openai/codex/blob/main/codex-rs/rollout/src/policy.rs)
- `CompactedItem`: `message`, `replacement_history`, `retained_context`, `window_number`. — [history/src/lib.rs](https://github.com/openai/codex/blob/main/codex-rs/history/src/lib.rs)

**OpenTelemetry**
- Configured with `[otel]` (`exporter`, `none` records but sends nothing). Events: `codex.conversation_starts`, `codex.api_request`, `codex.sse_event`, `codex.websocket_request/event`, `codex.user_prompt` (content redacted unless enabled), `codex.tool_decision`, `codex.tool_result` ("duration, success, output snippet"). — [Codex advanced config](https://developers.openai.com/codex/config-advanced)

### Inferences
- For Codex, the rollout's `patch_apply_end.changes` (with add/delete full content and update diffs) and `exec_command_end` (`aggregated_output`, `exit_code`) give results plus before and after impact without hooks. `turn_diff` gives the net per-turn diff.
- Hooks give real-time interception with CC-compatible payloads, so one CC-style hook adapter can serve both apps with small field differences (`turn_id`, `model`; no `PostToolUseFailure`).
- The rollout wire format appears as `{"timestamp", "type":"session_meta|response_item|event_msg|turn_context|compacted|...", "payload":{...}}`. The Rust source flattens `RolloutItem` into the line, but I did not locate the exact tag/content attribute in the file I fetched (§Gaps).

### Gaps
- Exact JSON tag names for `RolloutItem` on the wire (`type`/`payload`) are not confirmed from source; this is widely reported but unverified here.
- The full attribute list for `codex.tool_result` was only summarized in the docs.
- I did not fetch the AGENTS.md semantics doc for Codex. AGENTS.md is the instruction file (CC also reads AGENTS.md, per [CC memory](https://code.claude.com/docs/en/memory)), but Codex's discovery and merge order was not verified.

---

## 4. Gemini CLI: hooks, telemetry, checkpointing, chat storage

### Takeaway
Gemini CLI has hooks at the agent, tool and model layers. `BeforeModel`/`AfterModel` expose the full LLM request and response, which is unique among these apps. `AfterTool` gives `tool_response` (`llmContent`, `returnDisplay`, `error`). OTel `tool_call` includes `function_args` and line-change metadata. Checkpointing takes shadow-git snapshots before each file-modifying tool.

### Cited Findings
**Hooks**
- Base input fields: `session_id, transcript_path, cwd, hook_event_name, timestamp`. Only `type:"command"` handlers are supported. The default timeout is 60000 ms. — [Gemini hooks reference](https://github.com/google-gemini/gemini-cli/blob/main/docs/hooks/reference.md)
- Event fields:
  - **BeforeTool**: `tool_name, tool_input, mcp_context, original_request_name`.
  - **AfterTool**: adds `tool_response` `{llmContent, returnDisplay, error?}`; it can hide the result or request a `tailToolCallRequest`.
  - **BeforeAgent**: `prompt`. **AfterAgent**: `prompt, prompt_response, stop_hook_active`.
  - **BeforeModel**: `llm_request {model, messages, config}`. **AfterModel**: `llm_request, llm_response`, fired per streaming chunk.
  - **BeforeToolSelection** is also available.
  - **SessionStart**: `source startup|resume|clear`. **SessionEnd**: `reason exit|clear|logout|prompt_input_exit|other`; the CLI does not wait for it.
  - **Notification**: `notification_type:"ToolPermission"`.
  - **PreCompress**: `trigger auto|manual`; advisory and async.
  - Source: [Gemini hooks reference](https://github.com/google-gemini/gemini-cli/blob/main/docs/hooks/reference.md)
- MCP tools are named `mcp_<server_name>_<tool_name>`, with single underscores, unlike CC and Codex. Built-in tools include `read_file`, `run_shell_command`, `write_file` and `replace`. — [Gemini hooks reference](https://github.com/google-gemini/gemini-cli/blob/main/docs/hooks/reference.md); [Gemini checkpointing](https://github.com/google-gemini/gemini-cli/blob/main/docs/cli/checkpointing.md)

**Telemetry**
- `gemini_cli.tool_call` attributes: `function_name, function_args, duration_ms, success, decision (accept|reject|auto_accept|modify), error, error_type, prompt_id, tool_type (native|mcp), mcp_server_name, extension_name, content_length`, plus `metadata` (`model_added_lines, model_removed_lines, user_added_lines, user_removed_lines`).
- `gemini_cli.file_operation`: `tool_name, operation (create|read|update), lines, mimetype, extension, programming_language`.
- `gemini_cli.chat_compression`: `tokens_before, tokens_after`.
- `gemini_cli.user_prompt` includes `prompt` unless `logPrompts=false` (the default is `true`). Output to a local file is possible via `outfile`.
- Source: [Gemini telemetry](https://github.com/google-gemini/gemini-cli/blob/main/docs/cli/telemetry.md)

**Checkpointing and chat storage**
- Checkpointing is off by default and enabled with `general.checkpointing.enabled`. Before an approved file-modifying tool runs, it commits to a shadow git repo at `~/.gemini/history/<project_hash>` and saves the conversation and the pending tool call as JSON in `~/.gemini/tmp/<project_hash>/checkpoints`. `/restore` reverts files and conversation. — [Gemini checkpointing](https://github.com/google-gemini/gemini-cli/blob/main/docs/cli/checkpointing.md)
- Sessions are stored in `~/.gemini/tmp/<project_hash>/chats/`. — [Gemini session management](https://github.com/google-gemini/gemini-cli/blob/main/docs/cli/session-management.md)

### Inferences
- Gemini's shadow-git checkpoint is the strongest built-in "before" snapshot of any app checked: it covers the whole project tree. A continuity layer could read those commits for exact before and after state.

### Gaps
- I did not verify the exact chat JSON schema in `chats/`, or whether `AfterTool` fires on tool failure.

---

## 5. Cursor: hooks and transcripts

### Takeaway
Cursor hooks (version 1 config in `hooks.json`) cover sessions, generic tools, shell, MCP, file read and edit, prompts, responses, thoughts, subagents, compaction and stop.
- Results available: `afterShellExecution` gives full terminal `output`; `afterMCPExecution` gives `result_json`; `postToolUse` gives `tool_output`.
- File edits: `afterFileEdit` gives only `{file_path, edits:[{old_string,new_string}]}`, not full before and after content.
- Transcripts exist at `transcript_path` / `CURSOR_TRANSCRIPT_PATH` when enabled.

### Cited Findings
- Events: `sessionStart/sessionEnd`, `preToolUse/postToolUse/postToolUseFailure`, `subagentStart/subagentStop`, `beforeShellExecution/afterShellExecution`, `beforeMCPExecution/afterMCPExecution`, `beforeReadFile/afterFileEdit`, `beforeSubmitPrompt`, `preCompact`, `stop`, `afterAgentResponse/afterAgentThought`, `beforeTabFileRead/afterTabFileEdit`, `workspaceOpen`. Several are not supported for cloud agents. — [Cursor hooks](https://cursor.com/docs/agent/hooks)
- Common fields: `conversation_id`, `generation_id` (changes every user message), `model`, `model_id`, `model_params`, `hook_event_name`, `cursor_version`, `workspace_roots`, `user_email`, `transcript_path` (null if transcripts are disabled). — [Cursor hooks](https://cursor.com/docs/agent/hooks)
- Per-event fields:
  - `postToolUse`: `duration`, `tool_output` ("JSON-stringified result payload… not raw terminal text").
  - `postToolUseFailure`: `error_message, failure_type (error|timeout|permission_denied), duration, is_interrupt`.
  - `afterShellExecution`: `command, output` ("Full output captured from the terminal"), `duration, sandbox`.
  - `afterMCPExecution`: `tool_name, tool_input, mcp_server_name, mcp_server_url, result_json, duration`.
  - `afterFileEdit`: `file_path, edits[{old_string,new_string}]`.
  - `beforeReadFile`: `file_path, content, attachments`.
  - Source: [Cursor hooks](https://cursor.com/docs/agent/hooks)
- Subagent and lifecycle fields:
  - `subagentStart`: `subagent_id, subagent_type, task, parent_conversation_id, tool_call_id, subagent_model, is_parallel_worker, git_branch`.
  - `subagentStop`: `status, task, description, summary, duration_ms, message_count, tool_call_count, loop_count, modified_files, agent_transcript_path`.
  - `preCompact`: `trigger, context_usage_percent, context_tokens, context_window_size, message_count, messages_to_compact, is_first_compaction`; observational only.
  - `sessionEnd`: `reason (completed|aborted|error|window_close|user_close), duration_ms, final_status`.
  - `afterAgentResponse`: `text`. `afterAgentThought`: `text, duration_ms`. `stop`: `status, loop_count`.
  - Source: [Cursor hooks](https://cursor.com/docs/agent/hooks)
- MCP tools in tool-hook matchers use the format `MCP:<tool_name>`. Tool types include `Shell, Read, Write, Grep, Delete, Task`. Hook config has a `failClosed` option, and `loop_limit` defaults to 5 (null for Claude Code hooks). — [Cursor hooks](https://cursor.com/docs/agent/hooks)

### Inferences
- Cursor's config notes a separate `loop_limit` default "for Claude Code hooks", which suggests Cursor can load Claude-Code-format hooks. I did not verify this further.
- For a before-image of an edit in Cursor, the layer must snapshot the file itself (for example on `beforeReadFile`/`preToolUse`) or rebuild it from `edits`.

### Gaps
- Cursor's transcript file format (the example path ends `.txt`) is not documented in what I fetched.
- I did not check whether hook tool names match Claude Code's tool names.

---

## 6. Cline / Roo Code, Aider, OpenHands, Continue, local models

### Takeaway
These apps mainly expose **persisted history files** rather than rich hook APIs.
- Roo and Cline: per-task JSON (`api_conversation_history.json`, `ui_messages.json`, `task_metadata.json`).
- Aider: markdown chat log, input history, and an optional raw LLM log.
- OpenHands: a typed event stream with condenser events.
- Continue: `dev_data` events sent to local files or HTTP.

Before and after file content generally has to be rebuilt by the capture layer (for example with git).

### Cited Findings
- **Roo Code** `GlobalFileNames`: `api_conversation_history.json`, `ui_messages.json`, `task_metadata.json`, `history_item.json`, `_index.json`, `mcp_settings.json`, `custom_modes.yaml`. — [Roo-Code src/shared/globalFileNames.ts](https://github.com/RooCodeInc/Roo-Code/blob/main/src/shared/globalFileNames.ts)
- **Cline** hooks documentation now redirects to "SDK Plugins". — [Cline hooks](https://docs.cline.bot/customization/hooks)
- **Aider** history files:
  - `--chat-history-file` (default `.aider.chat.history.md`)
  - `--input-history-file` (default `.aider.input.history`)
  - `--llm-history-file` ("Log the conversation with the LLM to this file")
  - `--restore-chat-history`
  - `--max-chat-history-tokens` ("Soft limit… after which summarization begins")
  - Repo map controls: `--map-tokens`, `--map-refresh (auto|always|files|manual)`.
  - Source: [Aider options](https://github.com/Aider-AI/aider/blob/main/aider/website/docs/config/options.md)
- **OpenHands** (software-agent-sdk): a Condenser turns the event history `View` into an LLM-ready view. `LLMSummarizingCondenser` emits a `Condensation` event with summary metadata; `NoOpCondenser` returns the view unchanged. — [OpenHands condenser docs](https://github.com/OpenHands/docs/blob/main/sdk/arch/condenser.mdx)
- **Continue** collects development data to `.continue/dev_data` by default. Custom destinations (HTTP endpoint or local dir) are configured under `data` in `config.yaml`, with versioned event schemas in `packages/config-yaml/src/schemas/data`. — [Continue development data](https://github.com/continuedev/continue/blob/main/docs/customize/deep-dives/development-data.mdx)

### Inferences
- For Ollama or other local models used through these IDE extensions, the capture surface is whatever the host extension exposes (the files above). The model runtime adds none. An OpenAI-compatible logging proxy in front of Ollama is the only model-agnostic way to capture raw requests and responses, but it would not see tool execution results that never reach the model.

### Gaps
- Cline's current hook or plugin event names and payloads, and its task directory file names on current `main`, were not verified (the old `src/core/storage/disk.ts` path 404'd).
- I did not verify Roo Code's memory bank (a community convention, not a core feature as far as I know), OpenHands' event JSON schema (Action/Observation fields), or Continue's dev-data event names and fields.

---

## 7. Cross-app summary: which surfaces give tool RESULTS and file before/after content

### Takeaway
Tool **results** are available in real time from CC (PostToolUse/PostToolUseFailure/PostToolBatch), Codex (PostToolUse), Gemini (AfterTool) and Cursor (postToolUse, afterShellExecution, afterMCPExecution). Full **before and after file content** is available only from persisted artifacts:
- CC transcript `toolUseResult.originalFile` plus `structuredPatch`, and `file-history/`
- Codex rollout `patch_apply_end.changes` and `turn_diff`
- Gemini shadow-git checkpoints

OTel exporters mostly carry metadata; content needs explicit opt-in flags, and Claude Code's `tool_result` OTel event never carries output.

### Cited Findings

| App | Real-time tool result | File before/after | Shell side-effect diff | Sources |
|---|---|---|---|---|
| Claude Code | Hook `tool_response` (structured), PostToolBatch (model-visible text), failure `error` | Transcript `originalFile` + `structuredPatch`; `file-history/` pre-edit snapshots | `bashEditDiff` (beta, git-only, mode-gated) | [hooks](https://code.claude.com/docs/en/hooks), [.claude dir](https://code.claude.com/docs/en/claude-directory) |
| Codex | Hook `tool_response`; rollout `exec_command_end.aggregated_output`, `exit_code` | Rollout `patch_apply_end.changes` (Add/Delete content, Update diff), `turn_diff` | Not documented | [hooks](https://developers.openai.com/codex/hooks), [protocol.rs](https://github.com/openai/codex/blob/main/codex-rs/protocol/src/protocol.rs) |
| Gemini CLI | AfterTool `tool_response {llmContent, returnDisplay, error}` | Shadow-git checkpoint before each modifying tool (opt-in) | Not documented | [hooks](https://github.com/google-gemini/gemini-cli/blob/main/docs/hooks/reference.md), [checkpointing](https://github.com/google-gemini/gemini-cli/blob/main/docs/cli/checkpointing.md) |
| Cursor | `postToolUse.tool_output`, `afterShellExecution.output`, `afterMCPExecution.result_json` | `afterFileEdit` edits (old/new strings only) | Not documented | [hooks](https://cursor.com/docs/agent/hooks) |

### Inferences
- A model-independent continuity layer should use three channels:
  1. Real-time hooks for intent and result, keyed by `tool_use_id` / `call_id`.
  2. Its own workspace snapshot (git tree hash or file hashes) around every write-capable tool call, to prove impact independently of the app.
  3. Post-hoc reconciliation from the app's transcript or rollout, for calls whose post-hook never fired (crash, cancel, timeout).
- This matches Continuum's "ambiguous writes are unknown" invariant.
- Claude Code, Codex and Cursor converge on CC-style event names. One adapter with per-app field maps is feasible; Gemini's names (BeforeTool/AfterTool/BeforeAgent/AfterAgent) and its MCP naming differ.

### Gaps
- None of the apps document an exactly-once or ordering guarantee for hook delivery. Codex runs async hooks concurrently and may reorder them; CC runs matching hooks in parallel. Capture must deduplicate and order by app IDs and timestamps.

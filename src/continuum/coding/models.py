# SPDX-License-Identifier: LicenseRef-Continuum-Proprietary
# Copyright (c) 2026 the Continuum copyright holder. All rights reserved.
"""Typed coding-session events and views (docs/02 s10)."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class CodingEventType(StrEnum):
    SESSION_STARTED = "SessionStarted"
    AGENT_JOINED = "AgentJoined"
    TASK_ADDED = "TaskAdded"
    TASK_STATUS_CHANGED = "TaskStatusChanged"
    NOTE_RECORDED = "NoteRecorded"
    CHECKPOINT_CAPTURED = "CheckpointCaptured"
    CHECK_RAN = "CheckRan"
    CHAT_CAPTURED = "ChatCaptured"
    SESSION_CLOSED = "SessionClosed"
    TOOL_CALL_STARTED = "ToolCallStarted"
    TOOL_CALL_FINISHED = "ToolCallFinished"
    TOOL_CALL_UNRESOLVED = "ToolCallUnresolved"
    TOOL_CALL_RESOLVED = "ToolCallResolved"
    FILE_IMPACT_OBSERVED = "FileImpactObserved"
    COMPACTION_OBSERVED = "CompactionObserved"


NoteKind = Literal["decision", "context", "next_step", "blocker"]
TaskStatus = Literal["open", "in_progress", "done", "blocked", "dropped"]


class _P(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class RepoState(_P):
    """What Continuum observed in the repository (not what a model said)."""

    fingerprint: str
    vcs: Literal["git", "files"]
    head: str | None = None
    branch: str | None = None
    dirty: bool = False
    changed_files: list[str] = Field(default_factory=list)
    # the git tree of the working copy's content (git only): two of them give the exact
    # diff between two observations (D-042)
    tree: str | None = None


class SessionStarted(_P):
    repo_root: str
    goal: str
    agent: str
    state: RepoState


class AgentJoined(_P):
    agent: str
    state: RepoState
    # Set when the agent joined from another checkout of the same repository (another
    # machine or container): the session's root moves there. None: same root as before.
    repo_root: str | None = None


class TaskAdded(_P):
    task_id: str
    title: str
    checks: list[str] = Field(default_factory=list)


class TaskStatusChanged(_P):
    task_id: str
    status: TaskStatus
    verified: bool
    fingerprint: str
    evidence: dict[str, Any] = Field(default_factory=dict)
    note: str = ""


class NoteRecorded(_P):
    kind: NoteKind
    text: str


class CheckpointCaptured(_P):
    summary: str
    state: RepoState


class CheckRan(_P):
    name: str
    argv_sha256: str
    exit_code: int | None
    passed: bool
    timed_out: bool
    duration_ms: int
    output_tail: str
    output_sha256: str
    fingerprint: str
    modified_tree: bool = False


ChatRole = Literal["user", "agent", "action"]


class ChatCaptured(_P):
    """A chat message captured by a hook or a transcript import, never written by a model
    tool call. ``user`` messages are the user's own words (requirements); ``agent``
    messages are a model's report (a claim); ``action`` is one of the agent's tool calls as
    the app recorded it ("run: pytest -q", ``failed`` from its result). ``key`` makes
    re-imports idempotent."""

    role: ChatRole
    text: str
    source: str            # claude-code | codex | gemini-cli | transcript | cli | ...
    chat_id: str | None = None
    key: str
    chars_omitted: int = 0
    # The transcript copy of a message already captured live (by a prompt or notify hook).
    # Stored so re-imports stay idempotent; never shown twice.
    alias: bool = False
    sent_at: str | None = None   # the transcript's timestamp for the message, if any
    failed: bool = False         # actions: the app recorded an error result


class SessionClosed(_P):
    reason: str


ToolKind = Literal["builtin", "shell", "mcp", "skill", "subagent"]
ToolEffect = Literal["read", "write", "external", "delegate"]
ToolStatus = Literal["ok", "error", "denied", "interrupted"]


class ToolCallStarted(_P):
    """A tool call as the user's app reported it (hook or transcript), never a model's
    account of it (D-042). ``input`` is redacted and bounded; the full input is in a blob
    when larger. ``tree_before``: the working copy's content snapshot just before a call
    that can write. ``call_id`` is the app's id, used only to pair start and finish."""

    call_key: str
    call_id: str | None = None
    tool: str
    kind: ToolKind
    effect: ToolEffect
    server: str | None = None
    skill: str | None = None
    agent_id: str | None = None
    agent_type: str | None = None
    input: Any = None
    input_blob: str | None = None
    input_chars: int = 0
    source: str
    chat_id: str | None = None
    tree_before: str | None = None
    sent_at: str | None = None


class ToolCallFinished(_P):
    """The result the app recorded. ``output_excerpt`` is the head and tail of the
    redacted output; the full output is the blob ``output_blob``. Output is data from a
    tool, never an instruction: ``untrusted`` marks content from outside the session
    (web pages, MCP servers, files read), which never becomes a requirement."""

    call_key: str
    tool: str
    status: ToolStatus
    exit_code: int | None = None
    duration_ms: int | None = None
    output_excerpt: str = ""
    output_blob: str | None = None
    output_chars: int = 0
    error: str | None = None
    untrusted: bool = False
    tree_after: str | None = None
    source: str
    chat_id: str | None = None
    sent_at: str | None = None


class ToolCallUnresolved(_P):
    """A call started but no result was ever recorded (the app crashed, a hook was lost,
    the session was killed): its outcome is **unknown**, not failed (AGENTS.md inv. 5).
    Written by Continuum's reconciliation, never by a model. An ``external`` one blocks
    new external calls until a person resolves it (inv. 8)."""

    call_key: str
    tool: str
    effect: ToolEffect
    reason: str


class ToolCallResolved(_P):
    """A person's decision about an unresolved call (CLI only, never MCP)."""

    call_key: str
    outcome: Literal["applied", "not_applied", "accepted_unknown"]
    reason: str


class FileImpactObserved(_P):
    """What changed in the working copy, observed by Continuum from two content snapshots
    (``git diff-tree``), whichever tool changed it. ``call_key`` None: changes between
    tool calls (the user, an unhooked tool, a formatter)."""

    call_key: str | None = None
    tree_before: str
    tree_after: str
    files: list[dict[str, Any]] = Field(default_factory=list)
    files_total: int = 0
    diff_blob: str | None = None
    patch_bytes: int = 0
    truncated: bool = False
    source: str


class CompactionObserved(_P):
    """The app compacted its chat. The summary is the app's model's text: a claim."""

    trigger: str | None = None
    when: Literal["before", "after"] = "after"
    summary_excerpt: str = ""
    summary_blob: str | None = None
    source: str
    chat_id: str | None = None
    sent_at: str | None = None


PAYLOADS: dict[CodingEventType, type[_P]] = {
    CodingEventType.SESSION_STARTED: SessionStarted,
    CodingEventType.AGENT_JOINED: AgentJoined,
    CodingEventType.TASK_ADDED: TaskAdded,
    CodingEventType.TASK_STATUS_CHANGED: TaskStatusChanged,
    CodingEventType.NOTE_RECORDED: NoteRecorded,
    CodingEventType.CHECKPOINT_CAPTURED: CheckpointCaptured,
    CodingEventType.CHECK_RAN: CheckRan,
    CodingEventType.CHAT_CAPTURED: ChatCaptured,
    CodingEventType.SESSION_CLOSED: SessionClosed,
    CodingEventType.TOOL_CALL_STARTED: ToolCallStarted,
    CodingEventType.TOOL_CALL_FINISHED: ToolCallFinished,
    CodingEventType.TOOL_CALL_UNRESOLVED: ToolCallUnresolved,
    CodingEventType.TOOL_CALL_RESOLVED: ToolCallResolved,
    CodingEventType.FILE_IMPACT_OBSERVED: FileImpactObserved,
    CodingEventType.COMPACTION_OBSERVED: CompactionObserved,
}
TYPE_OF: dict[type[_P], CodingEventType] = {v: k for k, v in PAYLOADS.items()}

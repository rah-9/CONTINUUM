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


ChatRole = Literal["user", "agent"]


class ChatCaptured(_P):
    """A chat message captured by a hook or a transcript import, never written by a model
    tool call. ``user`` messages are the user's own words (requirements); ``agent``
    messages are a model's report (a claim). ``key`` makes re-imports idempotent."""

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


class SessionClosed(_P):
    reason: str


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
}
TYPE_OF: dict[type[_P], CodingEventType] = {v: k for k, v in PAYLOADS.items()}

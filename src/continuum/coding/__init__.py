"""Coding-session continuity: a coding agent's work survives model switches and new chats.

The journal is runtime-owned and append-only; repository state is observed by
Continuum (git or file hashes), never taken from model claims; a task is
"verified done" only when human-registered checks passed on the current code.
"""

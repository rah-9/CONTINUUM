#!/usr/bin/env bash
# SPDX-License-Identifier: LicenseRef-Continuum-Proprietary
# Copyright (c) 2026 the Continuum copyright holder. All rights reserved.
# Environment setup script for ephemeral cloud agent containers (Claude Code on
# the web, Codex cloud, CI agents) and fresh Linux/macOS machines. Installs
# Continuum so `continuum mcp` and the `continuum coding` CLI are on PATH.
#
# Works on images whose system Python is "externally managed" (PEP 668) or older
# than 3.12: Continuum goes into its own uv-managed tool environment with Python 3.12.
# Coding sessions persist only if CONTINUUM_DATABASE_URL points at a PostgreSQL that
# outlives the container (store the URL as an environment secret, never in git).
#
# Optional: CONTINUUM_SOURCE (any pip requirement URL, default: the GitHub repo),
#           CONTINUUM_BIN_DIR (where to link `continuum`; default /usr/local/bin if
#           writable, else ~/.local/bin).
set -euo pipefail
SOURCE="${CONTINUUM_SOURCE:-git+https://github.com/rah-9/CONTINUUM}"
export PATH="$HOME/.local/bin:$PATH"

if ! command -v uv >/dev/null 2>&1; then
  python3 -m pip install --quiet --user uv 2>/dev/null \
    || python3 -m pip install --quiet --user --break-system-packages uv 2>/dev/null \
    || curl -LsSf https://astral.sh/uv/install.sh | sh
fi
uv tool install --quiet --force --python 3.12 "continuum[mcp] @ ${SOURCE}"

# Agents launch `continuum mcp` without sourcing shell profiles: link it onto a PATH
# directory they already search.
TOOL_BIN="$(uv tool dir --bin)"
BIN_DIR="${CONTINUUM_BIN_DIR:-}"
if [ -z "$BIN_DIR" ]; then
  if [ -w /usr/local/bin ]; then BIN_DIR=/usr/local/bin; else BIN_DIR="$HOME/.local/bin"; fi
fi
mkdir -p "$BIN_DIR"
if [ "$BIN_DIR" != "$TOOL_BIN" ]; then ln -sf "$TOOL_BIN/continuum" "$BIN_DIR/continuum"; fi

if [ -z "${CONTINUUM_DATABASE_URL:-}" ]; then
  echo "CONTINUUM_DATABASE_URL is not set: coding sessions will not survive this container" >&2
else
  "$BIN_DIR/continuum" db-init >/dev/null
fi
echo "continuum installed: $BIN_DIR/continuum" >&2

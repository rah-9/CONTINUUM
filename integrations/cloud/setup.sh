#!/usr/bin/env bash
# Environment setup script for ephemeral cloud agent containers (Claude Code on
# the web, Codex cloud, CI agents). Installs Continuum so `continuum mcp` and the
# `continuum coding` CLI are on PATH. Coding sessions persist only if
# CONTINUUM_DATABASE_URL points at a PostgreSQL that outlives the container
# (store the URL as an environment secret, never in git).
set -euo pipefail
python3 -m pip install --quiet "continuum[mcp] @ git+https://github.com/rah-9/CONTINUUM"
if [ -z "${CONTINUUM_DATABASE_URL:-}" ]; then
  echo "CONTINUUM_DATABASE_URL is not set: coding sessions will not survive this container" >&2
else
  continuum db-init >/dev/null
fi

#!/usr/bin/env bash
# Judge a finished project: the two registered checks plus the hidden chat-only judge.
#   judge.sh PROJECT_DIR
set -euo pipefail
PROJ="$(cd "$1" && pwd)"
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"
TRIAL_PROJ_SRC="$PROJ/src" PYTHONPATH="$HERE:$PROJ/src" "${TRIAL_PYTHON:-python3}" -m pytest \
  -q -p no:cacheprovider accept_e1_function.py accept_e2_cli.py judge_chat_requirements.py

#!/usr/bin/env bash
# Prepare the export-feature handoff trial.
#   setup.sh WORKDIR
# Copies this repository's HEAD into WORKDIR/proj (the project the agents work on),
# commits it as the baseline, and registers the hidden acceptance tests as human checks
# e1 and e2. Needs `continuum` on PATH, PostgreSQL, and CONTINUUM_DATABASE_URL (the
# journal); TRIAL_DATABASE_URL is the scratch database the tests create schemas in.
set -euo pipefail
WORK="$(mkdir -p "$1" && cd "$1" && pwd)"
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(git -C "$HERE" rev-parse --show-toplevel)"
PY="${TRIAL_PYTHON:-python3}"
rm -rf "$WORK/proj" && mkdir -p "$WORK/proj"
git -C "$REPO" archive HEAD | tar -x -C "$WORK/proj"
rm -rf "$WORK/proj/eval/handoff_trials"          # the agents must not see the tests
git -C "$WORK/proj" init -q
git -C "$WORK/proj" -c user.email=trial@example.com -c user.name=trial add -A
git -C "$WORK/proj" -c user.email=trial@example.com -c user.name=trial commit -qm baseline
for check in e1:accept_e1_function e2:accept_e2_cli; do
  continuum coding checks add "$WORK/proj" "${check%%:*}" --actor trial --timeout 300 -- \
    /usr/bin/env TRIAL_PROJ_SRC="$WORK/proj/src" PYTHONPATH="$HERE:$WORK/proj/src" \
    "$PY" -m pytest -q -p no:cacheprovider "$HERE/${check#*:}.py" >/dev/null
done
echo "project: $WORK/proj  (baseline: $(git -C "$WORK/proj" rev-parse --short HEAD))"
echo "next: run model A with prompt_A.md, then snapshot: cp -a $WORK/proj $WORK/proj_b0"

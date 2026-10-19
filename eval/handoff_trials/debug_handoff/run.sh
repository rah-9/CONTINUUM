#!/usr/bin/env bash
# SPDX-License-Identifier: LicenseRef-Continuum-Proprietary
# Copyright (c) 2026 the Continuum copyright holder. All rights reserved.
# Trial 6, a debugging handoff.
#   run.sh WORKDIR CLAUDE_WRAPPER a                 model A: 3 turns in one Claude Code chat
#   run.sh WORKDIR CLAUDE_WRAPPER b MODEL ARM       model B: ARM = continuum | baseline
# Model A has only Continuum's Claude Code hooks (no rules, no MCP, no mention of it). It
# diagnoses, does not fix, and is stopped. Model B starts a new chat in another checkout of
# the same repository with "Continue the work on this repository.": with Continuum, the
# session-start hook gives it the brief; the baseline has no hooks. A's transcript is moved
# out of reach before any B runs. Each B gets its own copy of A's database schema.
set -euo pipefail
WORK="$(mkdir -p "$1" && cd "$1" && pwd)"; CL="$2"; PHASE="$3"
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(git -C "$HERE" rev-parse --show-toplevel)"
DB=postgresql://continuum:continuum@localhost:5432/continuum
A_SCHEMA=trial6_a
git_c() { git -C "$1" -c user.email=t@e.x -c user.name=t "${@:2}"; }

if [ "$PHASE" = a ]; then
  P="$WORK/proj"
  rm -rf "$P" && mkdir -p "$P/.claude"
  git -C "$P" init -q -b main
  git -C "$P" remote add origin https://example.com/acme/invoicer.git
  python3 "$HERE/fixture.py" "$P"
  python3 - "$REPO/integrations/claude-code/hooks/hooks.json" "$P/.claude/settings.json" <<'PY'
import json, sys
json.dump({"hooks": json.load(open(sys.argv[1]))["hooks"]}, open(sys.argv[2], "w"), indent=2)
PY
  git_c "$P" add -A && git_c "$P" commit -qm "invoicer"
  psql "$DB" -qc "DROP SCHEMA IF EXISTS $A_SCHEMA CASCADE" 2>/dev/null
  mkdir -p "$WORK/msgs"
  python3 - "$HERE/conversation.txt" "$WORK/msgs" <<'PY'
import sys
for i, m in enumerate(open(sys.argv[1]).read().split("\n---\n"), 1):
    open(f"{sys.argv[2]}/{i:02d}.txt", "w").write(m.strip() + "\n")
PY
  cd "$P"; SID=""
  for f in "$WORK"/msgs/*.txt; do
    n=$(basename "$f" .txt)
    extra=(); [ -n "$SID" ] && extra=(--resume "$SID")
    "$CL" CONTINUUM_DB_SCHEMA=$A_SCHEMA CONTINUUM_HOME="$WORK/home" claude -p "$(cat "$f")" \
      "${extra[@]}" --model sonnet --dangerously-skip-permissions --output-format json \
      > "$WORK/a_$n.json" 2> "$WORK/a_$n.err" || true
    SID=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['session_id'])" "$WORK/a_$n.json")
    echo "A turn $n done"
  done
  echo "$SID" > "$WORK/a_session_id"
  git -C "$P" status --porcelain > "$WORK/a_changes.txt"   # A was told not to change code
  pg_dump "$DB" -n $A_SCHEMA > "$WORK/a_schema.sql"
  # out of reach of every B: A's transcripts
  mkdir -p "$WORK/hidden"
  for d in /root/.claude/projects/*; do
    case "$d" in *"$(echo "$P" | tr '/.' '--')"*) mv "$d" "$WORK/hidden/" ;; esac
  done
  echo "A finished; changes: $(wc -l < "$WORK/a_changes.txt") file(s)"
  exit 0
fi

MODEL="$4"; ARM="$5"; NAME="b_${ARM}_${MODEL}"; B="$WORK/$NAME"
rm -rf "$B" && cp -a "$WORK/proj" "$B"           # another checkout, same remote and branch
SCHEMA=trial6_${ARM}_${MODEL}
psql "$DB" -qc "DROP SCHEMA IF EXISTS $SCHEMA CASCADE" 2>/dev/null
if [ "$ARM" = continuum ]; then
  sed "s/\b$A_SCHEMA\b/$SCHEMA/g" "$WORK/a_schema.sql" | psql "$DB" -q >/dev/null
else
  rm -f "$B/.claude/settings.json"                 # no Continuum at all
fi
cd "$B"
start=$(date +%s)
"$CL" CONTINUUM_DB_SCHEMA=$SCHEMA CONTINUUM_HOME="$WORK/home_$NAME" claude \
  -p "Continue the work on this repository." --model "$MODEL" \
  --dangerously-skip-permissions --output-format json > "$WORK/$NAME.json" 2> "$WORK/$NAME.err" || true
echo $(( $(date +%s) - start )) > "$WORK/$NAME.seconds"
PY="$REPO/.venv/bin/python"; [ -x "$PY" ] || PY=python3     # needs pytest
"$PY" "$HERE/judge.py" "$B" > "$WORK/$NAME.judge.json"
# leak guard: B must not have read A's transcript or this trial's files
if grep -rqsE "hidden/|handoff_trials|conversation.txt|judge.py" /root/.claude/projects/*"$(echo "$B" | tr '/.' '--')"*/*.jsonl; then
  echo "LEAK: $NAME touched trial files" | tee "$WORK/$NAME.LEAK"
fi
python3 - "$WORK/$NAME.json" "$WORK/$NAME.judge.json" "$WORK/$NAME.seconds" <<'PY'
import json, sys
r, j = json.load(open(sys.argv[1])), json.load(open(sys.argv[2]))
u = r.get("usage", {})
print(f"{sys.argv[1].split('/')[-1][:-5]}: score {j['score']}  cost ${r.get('total_cost_usd', 0):.2f}"
      f"  turns {r.get('num_turns')}  {open(sys.argv[3]).read().strip()}s  "
      f"output tokens {u.get('output_tokens')}  failing: "
      + ", ".join(k for k, v in j["results"].items() if not v))
PY

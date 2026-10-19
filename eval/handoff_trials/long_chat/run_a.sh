#!/usr/bin/env bash
# Long-chat trial, model A: the user's 30 messages, one Claude Code turn each, in ONE chat.
#   run_a.sh WORKDIR CLAUDE_WRAPPER [MODEL]
# Only Continuum's Claude Code hooks are installed: no rules, no MCP, no mention of
# Continuum. CLAUDE_WRAPPER runs `claude` with the environment the hooks need.
set -euo pipefail
WORK="$(mkdir -p "$1" && cd "$1" && pwd)"; CL="$2"; MODEL="${3:-sonnet}"
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(git -C "$HERE" rev-parse --show-toplevel)"
P="$WORK/proj"
rm -rf "$P" && mkdir -p "$P/.claude"
git -C "$P" init -q -b main
git -C "$P" remote add origin https://example.com/acme/taskr.git
python3 - "$REPO/integrations/claude-code/hooks/hooks.json" "$P/.claude/settings.json" <<'PY'
import json, sys
json.dump({"hooks": json.load(open(sys.argv[1]))["hooks"]}, open(sys.argv[2], "w"), indent=2)
PY
printf '# taskr\n' > "$P/README.md"
git -C "$P" -c user.email=t@e.x -c user.name=t add -A
git -C "$P" -c user.email=t@e.x -c user.name=t commit -qm "start"
python3 - "$HERE/conversation.txt" "$WORK/msgs" <<'PY'
import os, sys
os.makedirs(sys.argv[2], exist_ok=True)
for i, m in enumerate(open(sys.argv[1]).read().split("\n---\n"), 1):
    open(f"{sys.argv[2]}/{i:02d}.txt", "w").write(m.strip() + "\n")
PY
cd "$P"
SID=""
for f in "$WORK"/msgs/*.txt; do
  n=$(basename "$f" .txt)
  if [ -z "$SID" ]; then
    "$CL" claude -p "$(cat "$f")" --model "$MODEL" --dangerously-skip-permissions \
      --output-format json > "$WORK/a_$n.json" 2> "$WORK/a_$n.err" || true
    SID=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['session_id'])" "$WORK/a_$n.json")
  else
    "$CL" claude -p "$(cat "$f")" --resume "$SID" --model "$MODEL" \
      --dangerously-skip-permissions --output-format json > "$WORK/a_$n.json" 2> "$WORK/a_$n.err" || true
  fi
  echo "turn $n done"
done
echo "$SID" > "$WORK/a_session_id"
echo finished

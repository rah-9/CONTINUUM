#!/usr/bin/env bash
# SPDX-License-Identifier: LicenseRef-Continuum-Proprietary
# Copyright (c) 2026 the Continuum copyright holder. All rights reserved.
# Trial 7, part 2: does what the agent did reach the next model?
#   handoff.sh WORKDIR CLAUDE_WRAPPER MODEL continuum|baseline
# A new chat in another checkout of the same repository (same remote). Neither arm has the
# tickets MCP server: the ticket id exists only in the tracker and in Continuum's journal.
set -euo pipefail
WORK="$(cd "$1" && pwd)"; CL="$2"; MODEL="$3"; ARM="$4"
HERE="$(cd "$(dirname "$0")" && pwd)"
DB=postgresql://continuum:continuum@localhost:5432/continuum
B="$WORK/b_${ARM}_${MODEL}"; SCHEMA="trial7_b_${ARM}_${MODEL}"
rm -rf "$B" && cp -a "$WORK/proj" "$B"
psql "$DB" -qc "DROP SCHEMA IF EXISTS $SCHEMA CASCADE" 2>/dev/null
if [ "$ARM" = continuum ]; then
  pg_dump "$DB" -n trial7 | sed "s/\btrial7\b/$SCHEMA/g" | psql "$DB" -q >/dev/null
else
  rm -f "$B/.claude/settings.json"
fi
# A's chat files out of reach
mkdir -p "$WORK/hidden"
for d in /root/.claude/projects/*; do
  case "$d" in *"$(echo "$WORK/proj" | tr '/._' '---')"*) mv "$d" "$WORK/hidden/" ;; esac
done
cd "$B"
"$CL" CONTINUUM_DB_SCHEMA=$SCHEMA CONTINUUM_HOME="$WORK/home_b_$ARM" claude \
  -p "$(cat "$HERE/prompt_b.md")" --model "$MODEL" --dangerously-skip-permissions \
  --output-format json > "$WORK/b_${ARM}_${MODEL}.json" 2> "$WORK/b_${ARM}_${MODEL}.err" || true
python3 - "$WORK/b_${ARM}_${MODEL}.json" <<'PY'
import json, re, sys
r = json.load(open(sys.argv[1])); a = r.get("result", "")
checks = {
    "failing test (test_invoice_eu)": "test_invoice_eu" in a,
    "files changed (parse.py, rates_table.py, RELEASE_NOTES.md)":
        all(f in a for f in ("parse.py", "rates_table", "RELEASE_NOTES")),
    "ticket id (TCK-100)": "TCK-100" in a,
    "subagent's TODOs (CHF, VAT)": "CHF" in a and re.search(r"VAT", a) is not None,
}
print(f"{sys.argv[1].split('/')[-1]}: {sum(checks.values())}/4  cost ${r.get('total_cost_usd', 0):.3f}")
for k, v in checks.items():
    print(f"  {'yes' if v else 'NO '} {k}")
PY

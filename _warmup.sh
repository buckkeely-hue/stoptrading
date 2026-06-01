#!/bin/bash
cd /Users/buckk/stoptrading
# Chronological trading days (oldest first) for May 2026, excluding Memorial Day 5/25.
DAYS="2026-05-01 2026-05-04 2026-05-05 2026-05-06 2026-05-07 2026-05-08 2026-05-11 2026-05-12 2026-05-13 2026-05-14 2026-05-15 2026-05-18 2026-05-19 2026-05-20 2026-05-21 2026-05-22 2026-05-26 2026-05-27 2026-05-28 2026-05-29"
echo "WARM-UP START $(date)"
rm -f predictor_model.json predictor_data.jsonl   # fresh seed
for d in $DAYS; do
  python3 replay.py --date "$d" --seed-model --speed 3000 > "/tmp/warmup_$d.log" 2>&1
  rc=$?
  n=$(python3 -c "import json,os; print(json.load(open('predictor_model.json')).get('n_trained',0) if os.path.exists('predictor_model.json') else 0)" 2>/dev/null)
  realized=$(grep -m1 "Realized P&L" "/tmp/warmup_$d.log" | sed 's/.*Realized P&L//' | awk '{print $1$2}')
  if grep -q "No 1-min data" "/tmp/warmup_$d.log"; then
     echo "  $d : no data (skipped)"
  else
     echo "  $d : trained n=$n | day realized $realized (rc=$rc)"
  fi
done
echo "WARM-UP DONE $(date)"
python3 - <<'PY'
from modules.predictor import Predictor
p = Predictor({})
import json
print("=== SEEDED MODEL ==="); print(json.dumps(p.stats(), indent=2))
PY

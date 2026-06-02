#!/bin/bash
cd /Users/buckk/stoptrading
DAYS="2026-05-01 2026-05-04 2026-05-05 2026-05-06 2026-05-07 2026-05-08 2026-05-11 2026-05-12 2026-05-13 2026-05-14 2026-05-15 2026-05-18 2026-05-19 2026-05-20 2026-05-21 2026-05-22 2026-05-26 2026-05-27 2026-05-28 2026-05-29"
echo "WARM-UP START $(date)"
rm -f predictor_model.json predictor_data.jsonl predictor_exit_data.jsonl   # fresh seed: entry + exit
for d in $DAYS; do
  python3 replay.py --date "$d" --seed-model --speed 3000 > "/tmp/warmup_$d.log" 2>&1
  n=$(python3 -c "import json,os; print(json.load(open('predictor_model.json')).get('n_trained',0) if os.path.exists('predictor_model.json') else 0)" 2>/dev/null)
  xn=$(wc -l < predictor_exit_data.jsonl 2>/dev/null || echo 0)
  if grep -q "No 1-min data" "/tmp/warmup_$d.log"; then echo "  $d : no data (skipped)"
  else echo "  $d : entry n=$n | exit rows=$xn"; fi
done
echo "WARM-UP DONE $(date)"

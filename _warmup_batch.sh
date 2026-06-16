#!/bin/bash
cd /Users/buckk/stoptrading
DATES="2026-05-18 2026-05-19 2026-05-20 2026-05-21 2026-05-22 2026-05-26 2026-05-27 2026-05-28 2026-05-29 2026-06-01 2026-06-02 2026-06-03 2026-06-04 2026-06-05 2026-06-08 2026-06-09 2026-06-10 2026-06-11 2026-06-12"
mstate(){ python3 -c "import json;m=json.load(open('predictor_model.json'));print('n='+str(m['n_trained']),'wins='+str(m['wins']))"; }
echo "WARMUP START $(date)"
echo "before: $(mstate)"
for d in $DATES; do
  python3 replay.py --seed-model --date "$d" --speed 2000 > "_wu_$d.log" 2>&1
  buys=$(grep -cE "BUY +[A-Z]+ +[0-9]+ shares|BUY +[A-Z].*shares @" "_wu_$d.log" 2>/dev/null)
  echo "$d | $(mstate) | buys=$buys"
done
echo "after:  $(mstate)"
echo "=== replay-src base rate (clean training data) ==="
python3 -c "
import json,collections
n=w=0
for l in open('predictor_data.jsonl'):
    l=l.strip()
    if not l: continue
    d=json.loads(l)
    if d.get('src')=='replay':
        n+=1; w+=1 if (d.get('y',0) and d['y']>0.5) else 0
print(f'replay rows={n} wins={w} base={100*w/max(1,n):.1f}%')
"
echo "WARMUP DONE $(date)"

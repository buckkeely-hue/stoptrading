#!/bin/bash
cd /Users/buckk/stoptrading
echo "DATE        rvol  chg   trades  realized   MTM_PnL   deployed"
for d in 2026-05-28 2026-05-29 2026-05-27; do
  for combo in "1.5 3.0" "1.0 3.0" "1.0 2.0"; do
    set -- $combo; rv=$1; ch=$2
    L=/tmp/sw_${d}_${rv}_${ch}.log
    python3 replay.py --date "$d" --speed 4000 --min-rvol "$rv" --min-change "$ch" > "$L" 2>&1
    tr=$(grep -E "^  Trades " "$L" | head -1 | awk '{print $2}')
    rp=$(grep "Realized P&L" "$L" | head -1 | grep -oE '[-+]?\$ *[0-9.]+' | head -1 | tr -d ' ')
    mt=$(grep "Total P&L" "$L" | head -1 | grep -oE '[-+]?\$ *[0-9.]+' | head -1 | tr -d ' ')
    dp=$(grep "Capital deployed" "$L" | head -1 | grep -oE '\$ *[0-9.]+' | head -1 | tr -d ' ')
    printf "%-11s %-5s %-5s %-7s %-10s %-9s %s\n" "$d" "$rv" "$ch" "${tr:-?}" "${rp:-?}" "${mt:-?}" "${dp:-?}"
  done
done
echo "SWEEP DONE"

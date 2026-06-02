#!/bin/bash
# One-off: deterministic re-run of today's tape once yfinance settles it overnight.
cd /Users/buckk/stoptrading
DATE=2026-06-01
python3 replay.py --date "$DATE" --speed 3000 > "sandbox_${DATE}.log" 2>&1
sed -n '/REPLAY COMPARISON REPORT/,/Winner/p' "sandbox_${DATE}.log" > sandbox_report.txt
TOPIC=$(python3 -c "print(__import__('config').load_config().get('ntfy_topic',''))" 2>/dev/null)
SUMMARY=$(grep -E 'Total P&L|Trades|Winner|Realized' sandbox_report.txt | tr '\n' ' ')
[ -n "$TOPIC" ] && curl -s -H "Title: Deterministic sandbox $DATE" -d "$SUMMARY" "https://ntfy.sh/$TOPIC" >/dev/null
# one-off — remove self so it never re-fires
launchctl unload ~/Library/LaunchAgents/com.stoptrading.sandbox.plist 2>/dev/null
rm -f ~/Library/LaunchAgents/com.stoptrading.sandbox.plist

#!/bin/bash
# One-shot live smoke check of the StopTrading build — fired by launchd shortly after Monday's
# open (8:45 CDT = 9:45 ET). Verifies the behavioral layer a weekend static test can't see:
# loop ticking, predictor_gate rejecting + opening shadow-watches (learn-while-gated), feed health.
# Writes a timestamped report and pings via ntfy, then removes its own launchd job (one-shot).

cd /Users/buckk/stoptrading || exit 1
TS=$(date "+%Y-%m-%d %H:%M %Z")
DIR=/Users/buckk/stoptrading/smoke_reports
mkdir -p "$DIR"
REPORT="$DIR/smoke_$(date +%Y%m%d_%H%M).txt"
NTFY="https://ntfy.sh/stoptrading-474d3365352e"
PASS=1
NOW=$(date +%s)

occ() { grep -ho "$1" service.log decision_log.jsonl autopilot.json 2>/dev/null | wc -l | tr -d ' '; }

{
echo "StopTrading live smoke check — $TS"
echo "================================================================"

# 1. process + port
PID=$(pgrep -f "stoptrading/server.py" | head -1)
if [ -n "$PID" ] && lsof -ti :5175 >/dev/null 2>&1; then
  echo "[PASS] server up (pid $PID), :5175 listening"
else
  echo "[FAIL] server DOWN or :5175 not listening"; PASS=0
fi

# 2. autopilot loop ticking (decision_log written recently)
if [ -f decision_log.jsonl ]; then
  AGE=$(( NOW - $(stat -f %m decision_log.jsonl) ))
  ROWS=$(wc -l < decision_log.jsonl | tr -d ' ')
  if [ "$AGE" -lt 600 ]; then
    echo "[PASS] autopilot ticking — decision_log written ${AGE}s ago ($ROWS rows)"
  else
    echo "[WARN] decision_log stale (${AGE}s old) — loop idle or market not open yet ($ROWS rows)"; PASS=0
  fi
else
  echo "[FAIL] decision_log.jsonl missing"; PASS=0
fi

# 3. predictor learning (samples grow from the 79 build-time baseline as shadow-watches resolve)
[ -f predictor_data.jsonl ] && \
  echo "[INFO] predictor training samples: $(wc -l < predictor_data.jsonl | tr -d ' ')  (was 79 at build; growth = learning)"

# 4. gate + shadow-watch activity
G=$(occ "PREDICT-GATE"); S=$(occ "shadow-watch")
echo "[INFO] PREDICT-GATE mentions: $G | shadow-watch mentions: $S"
if [ "$S" -gt 0 ]; then echo "[PASS] gate rejecting + opening shadow-watches (learn-while-gated working)"
elif [ "$G" -gt 0 ]; then echo "[INFO] gate active; no shadow-watch lines yet (few candidates this early)"; fi

# 5. config sanity
python3 -c "import json;c=json.load(open('config.json'));print('[INFO] predictor_gate=%s  min_p=%s  shadow=%s'%(c.get('predictor_gate'),c.get('predictor_min_p'),c.get('predictor_shadow_watch','default-true')))" 2>/dev/null

# 6. errors + websocket health (recent log)
if [ -f service.log ]; then
  TB=$(tail -3000 service.log | grep -c "Traceback")
  WS=$(tail -3000 service.log | grep -c "connection lost")
  if [ "$TB" -gt 0 ]; then echo "[FAIL] $TB Python traceback(s) in recent log — investigate"; PASS=0
  else echo "[PASS] no Python tracebacks in recent log"; fi
  if [ "$WS" -gt 25 ]; then echo "[WARN] websocket 'connection lost' x$WS during hours — likely feed-credential issue to fix"
  else echo "[PASS] real-time feed stable ($WS reconnects in recent log)"; fi
fi

echo "================================================================"
[ "$PASS" -eq 1 ] && echo "RESULT: PASS — build healthy + behaving" || echo "RESULT: NEEDS ATTENTION (see flags above)"
} | tee "$REPORT"

# ping the user's phone via ntfy with the summary
SUMMARY=$(grep -E '^\[FAIL|^\[WARN|^RESULT|samples:' "$REPORT")
curl -s -H "Title: StopTrading Monday smoke check" -H "Tags: chart" \
     -d "$SUMMARY"$'\n'"full report: $REPORT" "$NTFY" >/dev/null 2>&1

# one-shot: remove own launchd job so it doesn't re-fire next Monday
rm -f /Users/buckk/Library/LaunchAgents/com.stoptrading.smokecheck.plist
launchctl bootout "gui/$(id -u)/com.stoptrading.smokecheck" 2>/dev/null
exit 0

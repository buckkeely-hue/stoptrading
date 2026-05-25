#!/bin/bash
set -e
cd "$(dirname "$0")"

pip3 install -r requirements.txt -q

echo ""
echo "Running test harness before launch..."
if ! python3 run_harness.py; then
    echo ""
    echo "ERROR: Test harness failed — server not started."
    echo "Fix the failures above or run: python3 run_harness.py <suite>"
    exit 1
fi

echo ""
echo "All checks passed — starting server."
python3 server.py

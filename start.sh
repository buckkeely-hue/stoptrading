#!/bin/bash
cd "$(dirname "$0")"

echo "StopTrading — Penny Stock Terminal"
echo "==================================="

# Check Python
if ! command -v python3 &>/dev/null; then
  echo "ERROR: python3 not found"
  exit 1
fi

# Install dependencies if needed
if ! python3 -c "import flask" 2>/dev/null; then
  echo "Installing dependencies..."
  pip3 install -r requirements.txt
fi

echo "Starting server at http://localhost:5175"
python3 server.py

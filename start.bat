@echo off
cd /d "%~dp0"

echo StopTrading — Penny Stock Terminal
echo ===================================

python --version >nul 2>&1
if errorlevel 1 (
  echo ERROR: Python not found. Install from python.org
  pause
  exit /b 1
)

python -c "import flask" >nul 2>&1
if errorlevel 1 (
  echo Installing dependencies...
  pip install -r requirements.txt
)

echo Starting server at http://localhost:5175
python server.py
pause

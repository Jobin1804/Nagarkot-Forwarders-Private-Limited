@echo off
REM Double-click to start the service, then open http://localhost:8000/docs
cd /d "%~dp0"
if not exist .venv (
  echo Run run_everything.bat first.
  pause
  exit /b 1
)
call .venv\Scripts\activate.bat
if not exist data\nfpl.db python -m app.seed
start "" http://localhost:8000/docs
python -m uvicorn app.main:app

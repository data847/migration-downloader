@echo off
rem Windows launcher for the migration-downloader web UI (http://127.0.0.1:8765).
rem Double-click this file. First run creates .venv and installs Flask + waitress.
rem Needs Python 3.10+ and Git on PATH (winget install -e --id Python.Python.3.12 / Git.Git).
setlocal
cd /d "%~dp0"

where git >nul 2>nul || (echo Git is not installed or not on PATH. Install it: winget install -e --id Git.Git & pause & exit /b 1)

if not exist ".venv\Scripts\python.exe" (
  echo creating .venv ...
  where py >nul 2>nul && (py -3 -m venv .venv) || (python -m venv .venv)
  if not exist ".venv\Scripts\python.exe" (echo Could not create .venv: install Python 3.12 from python.org or winget. & pause & exit /b 1)
  ".venv\Scripts\python.exe" -m pip install -q --upgrade pip
  ".venv\Scripts\python.exe" -m pip install -q -r requirements.txt || (echo pip install failed. & pause & exit /b 1)
)

if not exist ".env" (
  echo note: no .env next to this file - paste a token in the UI, or create .env with GITHUB_TOKEN=... / GITLAB_TOKEN=...
)

git config --global core.longpaths true >nul 2>nul

echo starting UI on http://127.0.0.1:8765  (close this window to stop)
start "" "http://127.0.0.1:8765"
".venv\Scripts\python.exe" app.py %*
pause

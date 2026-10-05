@echo off
chcp 65001 >nul
REM ============================================================
REM  Feishu Bot launcher (long connection, no public IP needed)
REM  Usage: start KVMem first, then double-click this file.
REM ============================================================
cd /d "%~dp0"

REM Python interpreter: use KVME_PY if set, else "python" on PATH.
REM (Hard-coding an absolute path here would leak the local user name
REM  and break for anyone who clones the repo elsewhere.)
if defined KVME_PY ( set "PY=%KVME_PY%" ) else ( set "PY=python" )

if not exist "%PY%" (
  echo [ERROR] python not found: %PY%  (set KVME_PY to override)
  pause
  exit /b 1
)

echo Starting Feishu Bot (long connection mode)...
echo Keep this window open. Press Ctrl+C to stop.
echo.
"%PY%" bot.py

pause

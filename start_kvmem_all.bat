@echo off
chcp 65001 >nul
setlocal
REM ============================================================
REM  KVMem one-click launcher  (ASCII-only on purpose)
REM  Why ASCII-only: this file contains Chinese comments, and cmd
REM  reads the file with the ANSI code page *before* chcp takes
REM  effect on later lines. Multi-byte chars then get split and the
REM  fragments ('uit)' / '' is not recognized ...) are executed as
REM  commands. Keeping this file pure ASCII removes the whole class
REM  of bugs. Chinese docs live in the .md files instead.
REM ============================================================

REM --- paths ------------------------------------------------------------
REM Everything is derived from the folder containing this script (%~dp0), so the repo
REM works from any drive or path after cloning. Override with env vars if
REM your weights or interpreter live elsewhere.
REM   KVME_ROOT      project root        (default: this scripts folder)
REM   KVME_MODEL     main model weights  (default: ROOT\models\...)
REM   KVME_MMPROJ    mmproj weights      (needed for vision)
REM   KVME_PY        main python         (default: python on PATH)
REM   KVME_QWEN_PY   router venv python  (default: ROOT\qwen\.venv\...)
REM ----------------------------------------------------------------------
if not defined KVME_ROOT set "KVME_ROOT=%~dp0"
if "%KVME_ROOT:~-1%"=="\" set "KVME_ROOT=%KVME_ROOT:~0,-1%"

set "ENGINE_BIN=%KVME_ROOT%\engine\bin"
set "ENGINE_EXE=%ENGINE_BIN%\llama-kvmem-server.exe"
if not defined KVME_MODEL set "KVME_MODEL=%KVME_ROOT%\models\Ternary-Bonsai-2-27B-PTQ1_0.gguf"
set "MODEL=%KVME_MODEL%"
set "MMPROJ=%KVME_MMPROJ%"
if not defined KVME_PY ( set "PY=python" ) else ( set "PY=%KVME_PY%" )
set "CTRL=%KVME_ROOT%\controller"
set "LOGDIR=%KVME_ROOT%"

REM Qwen intent router runs in its OWN venv (needs torch/transformers,
REM which conflict with the main python) and listens on port 18500.
if not defined QWEN_PY set "QWEN_PY=%KVME_ROOT%\qwen\.venv\Scripts\python.exe"
set "QWEN_DIR=%KVME_ROOT%\qwen"

title KVMem Service

echo ==================================================
echo   KVMem one-click launcher
echo   engine :18200    proxy :18201    qwen :18500
echo ==================================================

REM --- pre-cleanup: kill anything already on these ports (idempotent) ---
call :killport 18200
call :killport 18201
call :killport 18500

REM --- sanity checks: surface errors instead of silent flash ---
if not exist "%ENGINE_EXE%"  ( echo [ERROR] engine not found: %ENGINE_EXE%  & goto :fail )
if not exist "%MODEL%"       ( echo [ERROR] model not found: %MODEL%       & goto :fail )
if not exist "%PY%"          ( echo [ERROR] python not found: %PY%          & goto :fail )
if not exist "%MMPROJ%"      ( echo [ERROR] mmproj not found: %MMPROJ%      & goto :fail )
if not exist "%LOGDIR%\monitor.ps1" ( echo [ERROR] monitor.ps1 missing & goto :fail )
if not exist "%LOGDIR%\warmup.ps1"  ( echo [ERROR] warmup.ps1 missing  & goto :fail )

set "MMPROJARG="
if defined MMPROJ set "MMPROJARG=--mmproj %MMPROJ% --no-mmproj-offload"

REM --- engine (logs -> engine.log) ---
REM THINKING IS OFF BY DEFAULT (changed 2026-10-04).
REM Why: measured on this engine, thinking was 76 pct of all output chars
REM   (438 vs 135 chars of real answer) and a 3-word reply took 86s.
REM   The 04 doc records that thinking-mode presence-penalty defaults to
REM   0.0 (no repeat suppression) and its benefit was never verified,
REM   while "plan repetition" persisted 9/11 rounds even with it off.
REM   Engine flag --enable-thinking defaults off anyway; a request can
REM   still turn it on, which is what the Feishu "/think on" command does.
REM Keep --reasoning-budget 4096 so that when thinking IS enabled it
REM   still terminates gracefully (04 doc: do NOT hard-truncate thinking
REM   via -n or a client max_tokens).
cd /d "%ENGINE_BIN%"
start "ENGINE" /B cmd /c "%ENGINE_EXE% -m %MODEL% --host 127.0.0.1 --port 18200 -c 131072 -ngl 99 --kv-dtype q8_0 -ub 64 -b 256 %MMPROJARG% --kvmem-block-tokens 128 --kvmem-sink-tokens 4096 --kvmem-budget 16384 --kvmem-gen-reserve 16384 --kvmem-cpu-gb 6 --kvmem-method retrieval --kvmem-trace --reasoning-budget 4096 --presence-penalty 1.0 --spec-type none > %LOGDIR%\engine.log 2>&1"

REM --- dieting proxy (logs -> proxy.log) ---
cd /d "%CTRL%"
set "CONTROLLER_DIR=%CTRL%"
start "PROXY" /B cmd /c "%PY% %CTRL%\proxy_server.py > %LOGDIR%\proxy.log 2>&1"

REM --- Qwen intent router (:18500, optional) ---
REM Replaced Laya on 2026-10-04: Qwen3.5-0.8B scores 91 pct decision accuracy
REM on our 134-case Chinese benchmark vs Laya (57 pct), 0 safety misses.
REM bot degrades to plain chat if this is absent, so failure is non-fatal.
if exist "%QWEN_PY%" (
  cd /d "%QWEN_DIR%"
  start "QWEN" /B cmd /c "set HF_HUB_OFFLINE=1&& set TRANSFORMERS_VERBOSITY=error&& %QWEN_PY% %QWEN_DIR%\qwen_service.py > %LOGDIR%\qwen.log 2>&1"
  echo   Qwen intent router starting on :18500 (log: qwen.log, loads in 5-15s)
) else (
  echo   [SKIP] Qwen venv not found - bot runs in plain-chat mode. This is OK.
)

REM --- live monitor in its own window (read-only) ---
start "KVMem Monitor" "%PY%" "%LOGDIR%\monitor.py"

echo.
echo  Services starting. Monitor window opened.
echo    engine log : %LOGDIR%\engine.log
echo    proxy log  : %LOGDIR%\proxy.log
echo    qwen  log  : %LOGDIR%\qwen.log
echo    client URL : http://127.0.0.1:18201/v1
echo.

REM --- wait for engine ready, then warm it up (absorbs 16-19s cold-start
REM     prefill so the first real message is fast) ---
"%PY%" "%LOGDIR%\warmup.py"

echo.
echo  ==================================================
echo   ALL READY - client Base URL: http://127.0.0.1:18201/v1
echo  ==================================================
echo   To STOP: type "stop" here and press Enter,
echo            or just close this window (monitor closes itself).
echo.
:loop
set "CMD="
set /p CMD=  kvem^>
if /i "%CMD%"=="stop" goto :stop
if /i "%CMD%"=="status" goto :status
REM guard: avoid busy-spin if input stream is closed
ping -n 2 127.0.0.1 >nul
goto :loop

:status
"%PY%" "%LOGDIR%\experiments\launch.py" status
goto :loop

:stop
echo.
echo  Stopping services...
call :killport 18200
call :killport 18201
call :killport 18500
echo  Stopped. This window can be closed.
timeout /t 2 >nul
goto :eof

:fail
echo.
echo  Launch aborted - check the error above. Press any key to exit.
pause >nul
goto :eof

:killport
REM Delegate to launch.py stop-port. Why not inline for/f + taskkill:
REM   percent signs must be doubled inside a .bat, and getting that
REM   wrong yields "%%P was unexpected at this time" -- an error that
REM   looks like a syntax bug but is really an escaping bug.
REM   One Python entry point is easier to keep correct than four copies
REM   of the same netstat parsing.
"%PY%" "%LOGDIR%\experiments\launch.py" killport %1
goto :eof

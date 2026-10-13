@echo off
REM ===========================================================================
REM  CUSTOS - end-to-end verification (Windows)
REM
REM  Runs the same smoke test CI runs: authenticate, read the twin, force a
REM  cycle, launch a what-if ensemble, wait for it, read the recommendations,
REM  commit one, compare two runs, check that a fabricated figure is rejected,
REM  and verify the audit hash chain. It fails loudly, with the API's own error
REM  text, rather than printing a reassuring summary.
REM
REM  Usage:  scripts\smoke_windows.bat                     ^(against 127.0.0.1:8000^)
REM          scripts\smoke_windows.bat http://127.0.0.1:8100 --ensemble 8
REM ===========================================================================
setlocal

cd /d "%~dp0.."
set "ROOT=%CD%"
set "PY=%ROOT%\backend\.venv\Scripts\python.exe"
set "BASE=%~1"
if "%BASE%"=="" set "BASE=http://127.0.0.1:8000"
set "EXTRA=%~2 %~3 %~4 %~5"

if not exist "%PY%" (
    echo  [X] No virtual environment at backend\.venv - run scripts\setup_windows.bat first.
    exit /b 1
)

echo  CUSTOS smoke test against %BASE%
echo  ---------------------------------------------------------------
curl -s -o nul "%BASE%/api/v1/ready" >nul 2>nul
if errorlevel 1 (
    echo  [!] The API is not answering yet on %BASE%.
    echo      Start it with scripts\start_custos.bat and run this again.
    exit /b 1
)

"%PY%" "%ROOT%\backend\scripts\smoke.py" --base-url %BASE% --ensemble 12 %EXTRA%
set "EXITCODE=%ERRORLEVEL%"

echo.
if "%EXITCODE%"=="0" (
    echo  RESULT: PASS - the full operating loop works end to end.
) else (
    echo  RESULT: FAIL ^(exit %EXITCODE%^) - read the failing check above.
    echo  The check names map to the API endpoints in docs/RUNBOOK.md.
)
endlocal
exit /b %EXITCODE%

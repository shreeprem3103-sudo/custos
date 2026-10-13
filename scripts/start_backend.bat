@echo off
REM ===========================================================================
REM  CUSTOS - start the API, the engines and the console host (Windows)
REM
REM  Activates the isolated virtual environment and runs the API from it. The
REM  same entry point is used on Linux (backend/run_dev.sh), so the two
REM  platforms cannot drift apart.
REM
REM  Usage:  scripts\start_backend.bat            ^(port 8000, console enabled^)
REM          scripts\start_backend.bat 8100       ^(custom port^)
REM          scripts\start_backend.bat 8000 dev   ^(hot reload for API work^)
REM ===========================================================================
setlocal enabledelayedexpansion

cd /d "%~dp0.."
set "ROOT=%CD%"
set "PY=%ROOT%\backend\.venv\Scripts\python.exe"
set "PORT=%~1"
if "%PORT%"=="" set "PORT=8000"
set "RELOAD="
if /i "%~2"=="dev" set "RELOAD=--reload"

if not exist "%PY%" (
    echo  [X] No virtual environment at backend\.venv
    echo      Run scripts\setup_windows.bat first.
    exit /b 1
)
if not exist "%ROOT%\backend\.env.dev" (
    echo  [!] backend\.env.dev is missing - generating one with fresh secrets
    "%PY%" "%ROOT%\backend\scripts\bootstrap_env.py" || exit /b 1
)

echo  CUSTOS API starting on port %PORT% ...
echo    migrations/schema are applied automatically at boot
echo    the whole city is seeded on first run (117 assets, 464 edges, 40 sensors)
echo    console: http://127.0.0.1:%PORT%/console/
echo.

pushd "%ROOT%\backend"
"%PY%" run_dev.py --port %PORT% %RELOAD%
set "EXITCODE=%ERRORLEVEL%"
popd

echo.
if not "%EXITCODE%"=="0" (
    echo  [X] The API exited with code %EXITCODE%.
    echo      The last lines above say why: a blocked port, a missing dependency,
    echo      or a database that could not be opened.
) else (
    echo  CUSTOS API stopped.
)
endlocal
exit /b %EXITCODE%

@echo off
REM ===========================================================================
REM  CUSTOS - one-time setup on Windows
REM
REM  Creates an isolated Python virtual environment, installs the backend
REM  dependencies into it (never into the system Python), generates the local
REM  secrets file, installs the console's node modules and builds the static
REM  console bundle. Safe to re-run: every step is idempotent.
REM
REM  Usage:  scripts\setup_windows.bat
REM ===========================================================================
setlocal enabledelayedexpansion

cd /d "%~dp0.."
set "ROOT=%CD%"
set "VENV=%ROOT%\backend\.venv"
set "PY=%VENV%\Scripts\python.exe"

echo.
echo  CUSTOS setup  -  %ROOT%
echo  ---------------------------------------------------------------

REM --- 1. prerequisites ------------------------------------------------------
where python >nul 2>nul
if errorlevel 1 (
    echo  [X] Python 3.11+ was not found on PATH.
    echo      Install it from https://www.python.org/downloads/windows/ and
    echo      tick "Add python.exe to PATH", then run this script again.
    exit /b 1
)

where node >nul 2>nul
if errorlevel 1 (
    echo  [X] Node.js 20+ was not found on PATH.
    echo      Install the LTS build from https://nodejs.org/ then run this again.
    echo      ^(The API runs without Node; only the browser console needs it.^)
    exit /b 1
)

for /f "tokens=2" %%v in ('python --version 2^>^&1') do set "PYVER=%%v"
for /f "tokens=*" %%v in ('node --version') do set "NODEVER=%%v"
echo  [1/6] python %PYVER%   node %NODEVER%

REM --- 2. isolated virtual environment --------------------------------------
if exist "%PY%" (
    echo  [2/6] virtual environment already present: backend\.venv
) else (
    echo  [2/6] creating isolated virtual environment: backend\.venv
    python -m venv "%VENV%"
    if errorlevel 1 (
        echo  [X] Could not create the virtual environment.
        exit /b 1
    )
)

REM --- 3. backend dependencies ----------------------------------------------
echo  [3/6] installing backend dependencies ^(this can take a few minutes^)
"%PY%" -m pip install --upgrade pip --quiet
"%PY%" -m pip install -r "%ROOT%\backend\requirements.txt" --quiet
if errorlevel 1 (
    echo  [X] Dependency installation failed. Check the network/proxy settings.
    exit /b 1
)

REM --- 4. local secrets -----------------------------------------------------
if exist "%ROOT%\backend\.env.dev" (
    echo  [4/6] backend\.env.dev already exists - leaving your secrets alone
) else (
    echo  [4/6] generating backend\.env.dev with a fresh JWT secret and salted operators
    "%PY%" "%ROOT%\backend\scripts\bootstrap_env.py"
    if errorlevel 1 (
        echo  [X] Could not generate the environment file.
        exit /b 1
    )
)

REM --- 5. console dependencies ----------------------------------------------
pushd "%ROOT%\frontend"
if exist "node_modules" (
    echo  [5/6] console dependencies already installed
) else (
    echo  [5/6] installing console dependencies ^(npm install^)
    call npm install --no-audit --no-fund
    if errorlevel 1 (
        echo  [X] npm install failed.
        popd
        exit /b 1
    )
)

REM --- 6. build the static console ------------------------------------------
echo  [6/6] building the console bundle ^(served by the API at /console^)
call npm run build
if errorlevel 1 (
    echo  [X] Console build failed.
    popd
    exit /b 1
)
popd

echo  ---------------------------------------------------------------
echo  Setup complete.
echo.
echo    Start everything :  scripts\start_custos.bat
echo    API only         :  scripts\start_backend.bat
echo    Console dev mode :  scripts\start_frontend.bat
echo    Verify it all    :  scripts\smoke_windows.bat
echo.
echo  Sign in with:  commander.rao / custos-demo
echo  ^(Change the password in backend\.env.dev before any real deployment.^)
echo.
endlocal
exit /b 0

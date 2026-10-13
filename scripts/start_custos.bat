@echo off
REM ===========================================================================
REM  CUSTOS - start the whole platform on Windows
REM
REM  Boots, in order, and gates each step on the previous one being *ready*:
REM
REM    1. the API and engines        (isolated venv, port 8000)
REM    2. the console                (served by the API at /console, and the
REM                                   Next.js dev server on port 3000 when
REM                                   CUSTOS_DEV_SERVER=1 for UI work)
REM    3. the browser, pointed at the console
REM
REM  Everything runs in its own window so an operator can see the logs, and the
REM  script stops with a clear message rather than leaving half a system up.
REM
REM  Usage:  scripts\start_custos.bat            ^(API + console, one window each^)
REM          scripts\start_custos.bat /dev       ^(also run the Next dev server^)
REM ===========================================================================
setlocal enabledelayedexpansion

cd /d "%~dp0.."
set "ROOT=%CD%"
set "PY=%ROOT%\backend\.venv\Scripts\python.exe"
set "API_PORT=8000"
set "CONSOLE_URL=http://127.0.0.1:%API_PORT%/console/"
set "DEV_SERVER=0"
if /i "%~1"=="/dev" set "DEV_SERVER=1"

echo.
echo  CUSTOS - Crisis Digital Twin
echo  ===============================================================
echo   city      : Kochi ^(Ernakulam^) - single-city appliance
echo   profile   : MVP / development  ^(PostgreSQL is the production target^)
echo  ===============================================================
echo.

REM --- preflight -------------------------------------------------------------
if not exist "%PY%" (
    echo  [X] No virtual environment found.
    echo      Run scripts\setup_windows.bat first.
    exit /b 1
)
if not exist "%ROOT%\backend\.env.dev" (
    echo  [!] backend\.env.dev is missing - generating it now with fresh secrets
    "%PY%" "%ROOT%\backend\scripts\bootstrap_env.py" || (
        echo  [X] Could not generate the environment file.
        exit /b 1
    )
)

REM --- already running? ------------------------------------------------------
netstat -ano | findstr /r /c:":%API_PORT% .*LISTENING" >nul 2>nul
if not errorlevel 1 (
    echo  [!] Port %API_PORT% is already in use - CUSTOS may already be running.
    echo      Use scripts\stop_custos.bat first, or open %CONSOLE_URL%
    echo.
    choice /c YN /m "  Continue anyway"
    if errorlevel 2 exit /b 0
)

REM --- 1. API + engines ------------------------------------------------------
echo  [1/3] starting the API and the six-stage engine loop on port %API_PORT%
start "CUSTOS API" /D "%ROOT%\backend" cmd /k ""%PY%" run_dev.py --port %API_PORT%"

REM --- wait for readiness (not just for the port to open) --------------------
REM  A listening port means nothing: the API spends its first seconds applying
REM  the schema and seeding the city, and a console opened against a
REM  half-initialised twin shows an empty city. So we gate on /api/v1/ready.
set "HAVE_CURL=1"
where curl >nul 2>nul
if errorlevel 1 set "HAVE_CURL=0"
if "%HAVE_CURL%"=="0" (
    echo        [!] curl not found - falling back to a port check
    echo            ^(Windows 10 1803+ and Server 2019+ include curl.exe^)
)

set /a TRIES=0
:waitloop
set /a TRIES+=1
timeout /t 2 /nobreak >nul
if "%HAVE_CURL%"=="1" (
    curl -s -o nul --max-time 3 "http://127.0.0.1:%API_PORT%/api/v1/ready" >nul 2>nul
    if not errorlevel 1 goto ready
) else (
    netstat -ano | findstr /r /c:":%API_PORT% .*LISTENING" >nul 2>nul
    if not errorlevel 1 goto ready
)
if %TRIES% GEQ 45 (
    echo  [X] The API did not become ready within 90 seconds.
    echo      Look at the "CUSTOS API" window for the reason - the usual causes are
    echo      a blocked port or a dependency that failed to install.
    exit /b 1
)
set /a MOD=%TRIES% %% 5
if %MOD% EQU 0 echo        ...still starting ^(%TRIES% checks^)
goto waitloop

:ready
echo        API ready: http://127.0.0.1:%API_PORT%/api/docs

REM --- 2. console ------------------------------------------------------------
if "%DEV_SERVER%"=="1" (
    if not exist "%ROOT%\frontend\node_modules" (
        echo  [X] The dev server needs node_modules. Run scripts\setup_windows.bat first.
        exit /b 1
    )
    echo  [2/3] starting the console dev server on port 3000 ^(hot reload for UI work^)
    start "CUSTOS Console (dev)" /D "%ROOT%\frontend" cmd /k "npm run dev"
) else (
    if exist "%ROOT%\frontend\out\index.html" (
        echo  [2/3] console bundle found - the API is serving it at /console
    ) else (
        echo  [2/3] no console bundle yet - building it now ^(one-time, ~1 minute^)
        pushd "%ROOT%\frontend"
        call npm run build
        popd
        if not exist "%ROOT%\frontend\out\index.html" (
            echo  [!] Console build did not produce frontend\out - the API still works,
            echo      and http://127.0.0.1:%API_PORT%/console will explain what to run.
        )
    )
)

REM --- 3. open the console ---------------------------------------------------
echo  [3/3] opening the command console
start "" "%CONSOLE_URL%"

echo.
echo  ===============================================================
echo   API      : http://127.0.0.1:%API_PORT%/api/docs
echo   Console  : %CONSOLE_URL%
echo   Health   : http://127.0.0.1:%API_PORT%/api/v1/health
echo   Sign in  : commander.rao / custos-demo
echo.
echo   Stop     : scripts\stop_custos.bat
echo  ===============================================================
echo.
endlocal
exit /b 0

@echo off
REM ===========================================================================
REM  CUSTOS - start the command console (Windows)
REM
REM  Two ways to run the console, and this script handles both:
REM
REM    * "serve" (default) - build the static bundle and let the API serve it at
REM      /console. This is the deployment shape: one process, one port, no CORS,
REM      works air-gapped.
REM    * "dev" - the Next.js dev server on port 3000 with hot reload, proxying
REM      /api to the backend. For UI work only.
REM
REM  Usage:  scripts\start_frontend.bat          ^(build + serve via the API^)
REM          scripts\start_frontend.bat dev      ^(next dev on port 3000^)
REM ===========================================================================
setlocal enabledelayedexpansion

cd /d "%~dp0.."
set "ROOT=%CD%"
set "MODE=%~1"
if "%MODE%"=="" set "MODE=serve"

where node >nul 2>nul
if errorlevel 1 (
    echo  [X] Node.js 20+ is required for the console. The API runs without it.
    exit /b 1
)

pushd "%ROOT%\frontend"

if /i "%MODE%"=="dev" (
    if not exist "node_modules" (
        echo  [!] node_modules missing - installing now ^(npm install^)
        call npm install --no-audit --no-fund || (
            echo  [X] npm install failed.
            popd
            exit /b 1
        )
    )
    echo  Console dev server: http://localhost:3000/console/  ^(API on 8000^)
    call npm run dev
    set "EXITCODE=%ERRORLEVEL%"
    popd
    endlocal
    exit /b %EXITCODE%
)

if not exist "node_modules" (
    echo  [!] node_modules missing - installing now ^(npm install^)
    call npm install --no-audit --no-fund || (
        echo  [X] npm install failed.
        popd
        exit /b 1
    )
)

echo  Building the static console bundle ...
call npm run build
if errorlevel 1 (
    echo  [X] Build failed.
    popd
    exit /b 1
)
popd

if exist "%ROOT%\frontend\out\index.html" (
    echo  Console built: frontend\out
    echo  It is served by the API at http://127.0.0.1:8000/console/ once the API is up.
    echo  ^(Run scripts\start_backend.bat if it is not running yet.^)
) else (
    echo  [X] The build did not produce frontend\out\index.html
    exit /b 1
)
endlocal
exit /b 0

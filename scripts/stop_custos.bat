@echo off
REM ===========================================================================
REM  CUSTOS - stop everything (Windows)
REM
REM  Closes the windows this suite opens and frees the ports they hold. It
REM  refuses to kill a process it does not recognise, because the appliance may
REM  also be running PostgreSQL or a wall-display kiosk on the same box.
REM
REM  Usage:  scripts\stop_custos.bat
REM ===========================================================================
setlocal enabledelayedexpansion

echo.
echo  Stopping CUSTOS ...
echo.

REM --- the windows this suite starts ----------------------------------------
taskkill /FI "WINDOWTITLE eq CUSTOS API*" /T /F >nul 2>nul && echo   closed: API + engines window
taskkill /FI "WINDOWTITLE eq CUSTOS Console*" /T /F >nul 2>nul && echo   closed: console dev server window

REM --- anything still holding the ports -------------------------------------
for %%P in (8000 3000) do (
    set "PIDS="
    for /f "tokens=5" %%a in ('netstat -ano ^| findstr /r /c:":%%P .*LISTENING"') do set "PIDS=!PIDS! %%a"
    if not "!PIDS!"=="" (
        for %%i in (!PIDS!) do (
            echo   port %%P held by PID %%i - stopping it
            taskkill /PID %%i /T /F >nul 2>nul
        )
    )
)

echo.
echo  Stopped. The twin's state is on disk: the next start resumes the same city
echo  and replays the recorded event log, so nothing is lost by stopping cleanly.
echo.
endlocal
exit /b 0

@echo off
rem  Start the sidecar. It attaches in OBSERVE and cannot act until you arm it.
rem  Wraps: pz-agent start
rem  Arming is deliberately not wrapped in a double-clickable file: run
rem      bin\pz-agent.exe arm --mode assisted
rem  yourself, from a console, when you have decided to grant it authority.
setlocal
pushd "%~dp0"

set "PZ_AGENT=%~dp0bin\pz-agent.exe"
if exist "%PZ_AGENT%" goto :run
where pz-agent >nul 2>&1
if errorlevel 1 goto :nopz
set "PZ_AGENT=pz-agent"

:run
echo Start Project Zomboid and load your save before this, so the mod has a
echo session for the sidecar to attach to.
echo.
"%PZ_AGENT%" start %*
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" goto :failed
echo.
echo The sidecar is in OBSERVE: it watches and plans, and will not act.
echo Grant it authority with:  bin\pz-agent.exe arm --mode assisted
echo Take it back with:        bin\pz-agent.exe disarm
echo Stop it with:             stop.bat
popd
endlocal & exit /b 0

:failed
echo.
echo The sidecar did not start (exit code %RC%).
echo Read the reason printed above; doctor.bat explains the check codes.
echo A configuration error is reported without starting anything, so nothing
echo is running now.
popd
pause
endlocal & exit /b %RC%

:nopz
echo.
echo pz-agent.exe was not found.
echo Expected it at: %~dp0bin\pz-agent.exe
echo Unpack the whole release ZIP into one folder and run this file from there,
echo keeping bin\ next to it. If you installed from source instead, activate
echo the environment you installed into so that pz-agent is on your PATH.
popd
pause
endlocal & exit /b 9009

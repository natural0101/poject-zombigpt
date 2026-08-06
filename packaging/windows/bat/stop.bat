@echo off
rem  Ask a running sidecar to shut down.
rem  Wraps: pz-agent stop
rem  This is an orderly shutdown, not a panic stop. To stop the agent acting
rem  immediately, use  bin\pz-agent.exe disarm  or simply move your character:
rem  manual takeover ends the action in flight.
setlocal
pushd "%~dp0"

set "PZ_AGENT=%~dp0bin\pz-agent.exe"
if exist "%PZ_AGENT%" goto :run
where pz-agent >nul 2>&1
if errorlevel 1 goto :nopz
set "PZ_AGENT=pz-agent"

:run
"%PZ_AGENT%" stop %*
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" goto :failed
popd
endlocal & exit /b 0

:failed
echo.
echo Nothing was stopped (exit code %RC%).
echo The message above says whether no sidecar was running or the request could
echo not be delivered. status.bat shows what is attached.
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

@echo off
rem  Check the installation and the capability surface.
rem  Wraps: pz-agent doctor
rem  Anything you type after doctor.bat is passed to the command, e.g. --json.
setlocal
pushd "%~dp0"

set "PZ_AGENT=%~dp0bin\pz-agent.exe"
if exist "%PZ_AGENT%" goto :run
where pz-agent >nul 2>&1
if errorlevel 1 goto :nopz
set "PZ_AGENT=pz-agent"

:run
"%PZ_AGENT%" doctor %*
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" goto :failed
popd
endlocal & exit /b 0

:failed
echo.
echo doctor found something wrong (exit code %RC%).
echo Every check it printed has a code such as PZD003; look that code up in
echo docs\TROUBLESHOOTING.md, which says what causes it and what to do.
echo Nothing was changed on your machine.
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

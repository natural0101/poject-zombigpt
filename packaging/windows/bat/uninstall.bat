@echo off
rem  Remove exactly the files install.bat copied.
rem  Wraps: pz-agent uninstall-mod
rem  Saves, backups, logs and your configuration are never touched, and a file
rem  you edited after install is kept rather than deleted.
setlocal
pushd "%~dp0"

set "PZ_AGENT=%~dp0bin\pz-agent.exe"
if exist "%PZ_AGENT%" goto :run
where pz-agent >nul 2>&1
if errorlevel 1 goto :nopz
set "PZ_AGENT=pz-agent"

:run
"%PZ_AGENT%" uninstall-mod %*
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" goto :failed
echo.
echo The mod is removed. Your saves, backups and configuration are as they were.
popd
endlocal & exit /b 0

:failed
echo.
echo Nothing further was removed (exit code %RC%).
echo If it says there is no record of an install, the mod was not installed by
echo this tool; delete the folder yourself only if you are sure it is ours.
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

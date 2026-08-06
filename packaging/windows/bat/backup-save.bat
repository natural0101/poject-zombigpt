@echo off
rem  Take a hash-manifested copy of a save.
rem  Wraps: pz-agent backup-save
rem  With no arguments it backs up the save the agent resolves. Useful forms:
rem      backup-save.bat --list            list the backups you already have
rem      backup-save.bat Survivor/09-07-1993   back up one named save
rem  Restoring is a separate command on purpose:
rem      bin\pz-agent.exe restore-save <backup-id>
rem  and it refuses while Project Zomboid is open, because restoring over an
rem  open save destroys it.
setlocal
pushd "%~dp0"

set "PZ_AGENT=%~dp0bin\pz-agent.exe"
if exist "%PZ_AGENT%" goto :run
where pz-agent >nul 2>&1
if errorlevel 1 goto :nopz
set "PZ_AGENT=pz-agent"

:run
"%PZ_AGENT%" backup-save %*
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" goto :failed
popd
endlocal & exit /b 0

:failed
echo.
echo No backup was taken (exit code %RC%).
echo Nothing was written, so your save is untouched. If the message above says
echo no save was found, name one explicitly:
echo     backup-save.bat ^<mode^>/^<save name^>
echo backup-save.bat --list shows the ones already taken.
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

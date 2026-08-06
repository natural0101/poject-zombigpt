@echo off
rem  Copy the logs, journals and snapshots a scenario owes into its folder.
rem  Wraps: pz-agent live-test collect
rem  Useful forms:
rem      collect-evidence.bat                          every scenario that has run
rem      collect-evidence.bat --scenario S07_NESTED_INVENTORY
rem
rem  Run it while the game is still open, or at least before it is launched
rem  again: console.txt is rewritten on every launch, and the Lua error that
rem  explains a failure is only in the copy from the run that failed.
setlocal
pushd "%~dp0"

set "PZ_AGENT=%~dp0bin\pz-agent.exe"
rem  See run-live-tests.bat: the bundled executable cannot infer the evidence
rem  tree from its own location, so it is named here.
set "EVIDENCE=--evidence-dir "%~dp0evidence""
if exist "%PZ_AGENT%" goto :run
where pz-agent >nul 2>&1
if errorlevel 1 goto :nopz
set "PZ_AGENT=pz-agent"
set "EVIDENCE="

:run
"%PZ_AGENT%" live-test %EVIDENCE% collect %*
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" goto :failed
popd
endlocal & exit /b 0

:failed
echo.
echo Evidence was not fully collected (exit code %RC%).
echo Files that could not be copied are listed above by name - a missing
echo console.txt or ack journal is itself worth knowing, so nothing is skipped
echo silently. Whatever was copied has been hashed and kept.
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

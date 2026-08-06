@echo off
rem  Continue from the first scenario that is not PASS.
rem  Wraps: pz-agent live-test resume
rem  A scenario that already passed is not run again: its attempt ledger keeps
rem  the passing attempt, and resuming cannot overwrite it.
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
"%PZ_AGENT%" live-test %EVIDENCE% resume %*
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" goto :failed
popd
endlocal & exit /b 0

:failed
echo.
echo The resumed scenario did not pass (exit code %RC%).
echo Read which postcondition was not observed, collect the evidence with
echo collect-evidence.bat, fix that one cause, and run this again.
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

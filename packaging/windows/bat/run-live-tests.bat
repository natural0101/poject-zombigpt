@echo off
rem  Run the live scenarios. docs\LIVE_TEST_PLAYBOOK.md has one section each,
rem  in order, with the observations file every one of them needs.
rem  Wraps: pz-agent live-test run
rem  Useful forms:
rem      run-live-tests.bat --scenario S07_NESTED_INVENTORY --observations obs.json
rem      run-live-tests.bat --scenario S07_NESTED_INVENTORY
rem      run-live-tests.bat                            every scenario not yet PASS
rem
rem  --observations describes one scenario, so it must be given with --scenario:
rem  without it every pending scenario is selected and the run refuses rather
rem  than guess which one the file is about. Only that pair can produce a PASS -
rem  a run with nothing to observe is recorded as BLOCKED.
rem
rem  Start the game, load the TEST save and run start.bat first. A scenario the
rem  runner could not observe is recorded as BLOCKED, never as a pass.
setlocal
pushd "%~dp0"

set "PZ_AGENT=%~dp0bin\pz-agent.exe"
rem  The bundled executable unpacks itself into a temporary folder, so its idea
rem  of "the checkout I came from" is that folder. The evidence tree is named
rem  explicitly there; from a source install the default is already right.
set "EVIDENCE=--evidence-dir "%~dp0evidence""
if exist "%PZ_AGENT%" goto :run
where pz-agent >nul 2>&1
if errorlevel 1 goto :nopz
set "PZ_AGENT=pz-agent"
set "EVIDENCE="

:run
"%PZ_AGENT%" live-test %EVIDENCE% run %*
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" goto :failed
popd
endlocal & exit /b 0

:failed
echo.
echo The run did not finish with every scenario passing (exit code %RC%).
echo That is a result, not a crash: read the table above for which scenario and
echo which postcondition. Then:
echo   1. collect-evidence.bat --scenario S^<nn^>_^<NAME^>
echo   2. docs\LOCAL_DEBUG_MAP.md maps the symptom to a module and a log.
echo   3. Fix that one cause and run this again; resume-live-tests.bat continues
echo      from the first scenario that is not PASS.
echo Do not edit a result file. It is hashed, and an edit is detected at finalize.
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

@echo off
rem  Build the evidence manifest, or name everything that is missing.
rem  Wraps: pz-agent live-test finalize
rem
rem  It writes release\evidence-manifest.json only when every scenario in the
rem  catalogue is PASS and every artefact they owe is present and matches the digest
rem  recorded for it. Anything else is a refusal listing every problem at once;
rem  nothing partial is written, because a partial manifest is exactly what a
rem  release gate would accept by mistake.
setlocal
pushd "%~dp0"

set "PZ_AGENT=%~dp0bin\pz-agent.exe"
rem  See run-live-tests.bat. The manifest is named here too, so it lands beside
rem  the evidence it accounts for rather than inside a temporary folder.
set "EVIDENCE=--evidence-dir "%~dp0evidence""
set "OUTPUT=--output "%~dp0release\evidence-manifest.json""
if exist "%PZ_AGENT%" goto :run
where pz-agent >nul 2>&1
if errorlevel 1 goto :nopz
set "PZ_AGENT=pz-agent"
set "EVIDENCE="
set "OUTPUT="

:run
"%PZ_AGENT%" live-test %EVIDENCE% finalize %OUTPUT% %*
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" goto :failed
echo.
echo The manifest is written. The release gate reads it:
echo     python scripts\check_release.py --release
echo Tag v1.0.0 after that passes, not before.
popd
endlocal & exit /b 0

:failed
echo.
echo No manifest was written (exit code %RC%).
echo Every problem is listed above: scenarios that are not PASS, artefacts that
echo are missing, and any result whose bytes no longer match its recorded hash.
echo Fix them by running the scenarios, not by editing the files - an edited
echo result is what this check exists to catch.
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

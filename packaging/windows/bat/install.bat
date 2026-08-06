@echo off
rem  Install the bridge mod and place a starting configuration.
rem  Wraps: pz-agent install-mod --source <the mod shipped beside this file>
rem
rem  Writes only inside your own Zomboid folder and needs no administrator
rem  rights. It records every file it copies, so uninstall.bat removes exactly
rem  those and nothing else.
setlocal
pushd "%~dp0"

set "PZ_AGENT=%~dp0bin\pz-agent.exe"
if exist "%PZ_AGENT%" goto :find_mod
where pz-agent >nul 2>&1
if errorlevel 1 goto :nopz
set "PZ_AGENT=pz-agent"

:find_mod
rem  Two layouts: mod\ beside this file in the release ZIP, and pz-mod\42 when
rem  this file is run from a checkout of the repository.
set "MOD_SOURCE=%~dp0mod"
if exist "%MOD_SOURCE%\mod.info" goto :find_sample
set "MOD_SOURCE=%~dp0..\..\..\pz-mod\42"
if exist "%MOD_SOURCE%\mod.info" goto :find_sample
goto :nomod

:find_sample
set "CONFIG_SAMPLE=%~dp0configs\agent\config.example.toml"
if exist "%CONFIG_SAMPLE%" goto :install
set "CONFIG_SAMPLE=%~dp0..\..\..\configs\agent\config.example.toml"

:install
echo Installing the bridge mod from "%MOD_SOURCE%"
"%PZ_AGENT%" install-mod --source "%MOD_SOURCE%" %*
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" goto :failed

set "CONFIG_DIR=%USERPROFILE%\Zomboid\pz-agent"
set "CONFIG_FILE=%CONFIG_DIR%\config.toml"
if exist "%CONFIG_FILE%" goto :validate
if not exist "%USERPROFILE%\Zomboid" goto :validate
if not exist "%CONFIG_SAMPLE%" goto :validate
if not exist "%CONFIG_DIR%" mkdir "%CONFIG_DIR%"
copy "%CONFIG_SAMPLE%" "%CONFIG_FILE%" >nul
echo Placed a starting configuration at "%CONFIG_FILE%"

:validate
echo.
echo Checking the configuration the agent will actually read:
"%PZ_AGENT%" validate-config
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" goto :badconfig

echo.
echo Installed. Next:
echo   1. Start Project Zomboid and enable "PZ Agent Bridge" in the Mods menu.
echo   2. Load your save - enabling a mod does not affect a game already loaded.
echo   3. Run doctor.bat to check the installation.
echo   4. Run backup-save.bat before you let the agent act on a save you care about.
popd
endlocal & exit /b 0

:badconfig
echo.
echo The mod is installed, but the configuration the agent resolves is not
echo usable (exit code %RC%). The message above names the file and the key.
echo If it says the file does not exist, copy the sample there yourself:
echo     "%CONFIG_SAMPLE%"
echo doctor.bat prints every path this machine resolved.
popd
pause
endlocal & exit /b %RC%

:failed
echo.
echo The mod was not installed (exit code %RC%).
echo If it says a file is already there that pz-agent did not write, nothing was
echo copied: move that file, or run uninstall.bat first. If it says no Zomboid
echo folder was found, start Project Zomboid once so the game creates it.
popd
pause
endlocal & exit /b %RC%

:nomod
echo.
echo The mod files were not found next to this script.
echo Looked for mod.info in:
echo     %~dp0mod
echo     %~dp0..\..\..\pz-mod\42
echo Unpack the whole release ZIP into one folder, keeping mod\ next to this file.
popd
pause
endlocal & exit /b 1

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

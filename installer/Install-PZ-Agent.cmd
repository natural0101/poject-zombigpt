@echo off
setlocal
title PZ Agent Install
rem Thin wrapper. Every decision this makes lives in pz_agent_installer.py,
rem which is a plain stdlib Python script and is the thing the tests drive.
rem Needs no administrator rights and writes only under your Zomboid folder.

py -3 --version >nul 2>nul
if not errorlevel 1 (
  py -3 "%~dp0pz_agent_installer.py" install %*
  exit /b %errorlevel%
)

python --version >nul 2>nul
if not errorlevel 1 (
  python "%~dp0pz_agent_installer.py" install %*
  exit /b %errorlevel%
)

echo Python 3.11 or newer was not found on PATH.
echo Install it from https://www.python.org/downloads/windows/ and run this again.
endlocal
exit /b 1

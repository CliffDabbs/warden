@echo off
REM Warden launcher — used by the "Warden" scheduled task (and handy to run by hand).
REM Logs to data\warden.log; reads all config from .env.
cd /d "%~dp0"
if not exist "data" mkdir "data"
echo. >> "data\warden.log"
echo ==== Warden starting %DATE% %TIME% ==== >> "data\warden.log"
".venv\Scripts\python.exe" -m warden >> "data\warden.log" 2>&1
echo ==== Warden exited (code %ERRORLEVEL%) %DATE% %TIME% ==== >> "data\warden.log"

@echo off
echo Starting RhapsodyAIAgent Setup...
powershell -ExecutionPolicy Bypass -NoProfile -File "%~dp0setup.ps1"
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo Setup failed with error code %ERRORLEVEL%
    pause
) else (
    pause
)

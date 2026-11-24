@echo off
REM Rhapsody Model Assistant installer
REM   install.bat       install the newest .vsix next to this file (or in .\dist)
REM   install.bat dev   developer setup: run the extension from this repo checkout
REM                     (Python venv + pip, see setup.ps1)

if /I "%~1"=="dev" goto dev

powershell -ExecutionPolicy Bypass -NoProfile -File "%~dp0install_vsix.ps1"
set RC=%ERRORLEVEL%
if %RC%==0 goto done
if %RC%==2 goto novsix
echo.
echo VSIX installation failed with error code %RC%
pause
exit /b %RC%

:novsix
echo.
echo Download rhapsody-design-assistant-win32-x64-^<version^>.vsix from the repository's
echo Releases page (or Actions -^> Build VSIX -^> Artifacts) and put it next to install.bat.
echo.
set /p ANS=Or run the developer setup instead (Python venv + pip)? (y/n)
if /I "%ANS%"=="y" goto dev
pause
exit /b 2

:dev
echo Starting developer setup (runs the extension from this repo checkout)...
powershell -ExecutionPolicy Bypass -NoProfile -File "%~dp0setup.ps1"
set RC=%ERRORLEVEL%
if not %RC%==0 (
    echo.
    echo Setup failed with error code %RC%
    pause
    exit /b %RC%
)
REM setup.ps1 already waits for Enter
exit /b 0

:done
pause
exit /b 0

@echo off
rem Greenline bot launcher for Windows. Double-click this file.
rem Only ASCII here on purpose: cmd.exe misreads UTF-8 Cyrillic and may close silently.
cd /d "%~dp0"
if not exist "launcher.py" goto notextracted

set "PY="
py -3 --version >nul 2>nul && set "PY=py -3"
if not defined PY python --version >nul 2>nul && set "PY=python"
if not defined PY goto nopython

if not exist ".venv\Scripts\python.exe" echo Preparing Python environment, please wait...
if not exist ".venv\Scripts\python.exe" %PY% -m venv .venv
if not exist ".venv\Scripts\python.exe" goto venvfail

".venv\Scripts\python.exe" launcher.py %*
goto end

:notextracted
echo ERROR: the ZIP is not unpacked.
echo Right-click greenline-bot.zip - "Extract All...", open the "bot" folder and run start.bat there.
goto end

:nopython
echo Python is not installed.
echo Install it from https://www.python.org/downloads/
echo IMPORTANT: tick "Add python.exe to PATH" in the first installer window, then run start.bat again.
goto end

:venvfail
echo Could not create the Python environment (.venv). Send a screenshot of this window.

:end
echo.
pause

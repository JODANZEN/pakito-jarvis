@echo off
rem Get the latest Pakito from GitHub and install anything new. Your settings (config.local.json) stay as they are.
cd /d "%~dp0"
where git >nul 2>nul || (echo Git isn't installed: winget install Git.Git & pause & exit /b 1)
echo Getting the latest Pakito...
git pull --ff-only
if errorlevel 1 (
  echo.
  echo Couldn't update. Either there's no internet, or files here were changed by hand ^(git status shows which^).
  pause & exit /b 1
)
set "PY=%USERPROFILE%\miniconda3\envs\pakito\python.exe"
if not exist "%PY%" set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" (
  echo Run setup-pakito.bat first.
  pause & exit /b 1
)
echo Installing anything new...
"%PY%" -m pip install --disable-pip-version-check -q -r assistant\requirements.txt
"%PY%" -m assistant.check_live all
echo.
echo Updated. If Pakito is running, close it and start it again.
pause

@echo off
rem First-time setup: installs what Pakito needs, then asks you a few questions (Gemini key, mic, browser...).
rem Safe to run again any time.
cd /d "%~dp0"
call :findpy
if not defined PY (
  echo Python 3.10 or newer is needed. Install it from https://www.python.org/downloads/
  echo ^(tick "Add python.exe to PATH"^), then run this again.
  start "" https://www.python.org/downloads/
  pause & exit /b 1
)
echo Using Python: %PY%
echo Installing packages ^(a minute or two the first time^)...
"%PY%" -m pip install --disable-pip-version-check -q -r assistant\requirements.txt || (pause & exit /b 1)
echo.
"%PY%" -m assistant.setup_wizard
echo.
echo All set. Start Pakito with run-pakito.bat
pause
exit /b

:findpy
rem 1) Miniconda env "pakito" (create it if conda is there)  2) a .venv next to this file (create it from py / python)
set "PY="
set "CONDA=%USERPROFILE%\miniconda3\Scripts\conda.exe"
if exist "%USERPROFILE%\miniconda3\envs\pakito\python.exe" set "PY=%USERPROFILE%\miniconda3\envs\pakito\python.exe" & exit /b
if exist "%CONDA%" (
  echo Creating the "pakito" Python 3.12 environment...
  "%CONDA%" create -n pakito python=3.12 -y -c conda-forge --override-channels >nul
  if exist "%USERPROFILE%\miniconda3\envs\pakito\python.exe" set "PY=%USERPROFILE%\miniconda3\envs\pakito\python.exe"
)
if defined PY exit /b
if exist "%~dp0.venv\Scripts\python.exe" set "PY=%~dp0.venv\Scripts\python.exe" & exit /b
set "BASE="
where py >nul 2>nul && set "BASE=py -3"
if not defined BASE where python >nul 2>nul && set "BASE=python"
if not defined BASE exit /b
%BASE% -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" 2>nul || exit /b
echo Creating a private Python environment in .venv ...
%BASE% -m venv "%~dp0.venv" && set "PY=%~dp0.venv\Scripts\python.exe"
exit /b

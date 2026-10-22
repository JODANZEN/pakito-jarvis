@echo off
rem First-time setup: installs what Pakito needs, then asks you a few questions (Gemini key, mic, browser...).
rem Safe to run again any time.
cd /d "%~dp0"
call :findpy
if not defined PY goto nopython
echo Using Python: %PY%
echo Installing packages ^(a minute or two the first time^)...
"%PY%" -m pip install --disable-pip-version-check -q -r assistant\requirements.txt || (pause & exit /b 1)
echo.
"%PY%" -m assistant.setup_wizard
echo.
echo All set. Start Pakito with run-pakito.bat
pause
exit /b

:nopython
echo.
if defined OTHERPY (
  echo You have Python %OTHERPY%, but Pakito needs Python 3.10 to 3.13: its microphone library
  echo doesn't support newer versions yet. Install Python 3.13 next to it ^(no need to uninstall^).
) else (
  echo Pakito needs Python 3.10 to 3.13.
)
echo On the page that opens, pick the newest "Python 3.13.x" and its "Windows installer (64-bit)".
echo In the installer, tick "Add python.exe to PATH". Then run setup-pakito.bat again.
start "" https://www.python.org/downloads/windows/
pause
exit /b 1

:findpy
rem 1) Miniconda env "pakito"  2) an existing .venv made with a supported Python  3) a new .venv from py/python 3.10-3.13
set "PY="
set "OTHERPY="
set "OK=import sys; sys.exit(0 if (3, 10) <= sys.version_info[:2] <= (3, 13) else 1)"
set "CONDA=%USERPROFILE%\miniconda3\Scripts\conda.exe"
if exist "%USERPROFILE%\miniconda3\envs\pakito\python.exe" set "PY=%USERPROFILE%\miniconda3\envs\pakito\python.exe" & exit /b
if exist "%CONDA%" (
  echo Creating the "pakito" Python 3.12 environment...
  "%CONDA%" create -n pakito python=3.12 -y -c conda-forge --override-channels >nul
  if exist "%USERPROFILE%\miniconda3\envs\pakito\python.exe" set "PY=%USERPROFILE%\miniconda3\envs\pakito\python.exe"
)
if defined PY exit /b
if exist "%~dp0.venv\Scripts\python.exe" (
  "%~dp0.venv\Scripts\python.exe" -c "%OK%" 2>nul && set "PY=%~dp0.venv\Scripts\python.exe"
)
if defined PY exit /b
if exist "%~dp0.venv" (
  echo The .venv folder was made with a Python Pakito can't use. Making a new one...
  rmdir /s /q "%~dp0.venv"
)
set "BASE="
for %%v in (3.13 3.12 3.11 3.10) do (
  if not defined BASE py -%%v -c "%OK%" >nul 2>nul && set "BASE=py -%%v"
)
if not defined BASE (
  python -c "%OK%" >nul 2>nul && set "BASE=python"
)
if not defined BASE (
  for /f "delims=" %%i in ('py -3 -c "import sys; print(str(sys.version_info[0]) + chr(46) + str(sys.version_info[1]))" 2^>nul') do set "OTHERPY=%%i"
)
if not defined BASE if not defined OTHERPY (
  for /f "delims=" %%i in ('python -c "import sys; print(str(sys.version_info[0]) + chr(46) + str(sys.version_info[1]))" 2^>nul') do set "OTHERPY=%%i"
)
if not defined BASE exit /b
echo Creating a private Python environment in .venv with %BASE% ...
%BASE% -m venv "%~dp0.venv" && set "PY=%~dp0.venv\Scripts\python.exe"
exit /b

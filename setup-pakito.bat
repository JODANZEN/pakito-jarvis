@echo off
cd /d "%~dp0"
set "CONDA=%USERPROFILE%\miniconda3\Scripts\conda.exe"
set "PY=%USERPROFILE%\miniconda3\envs\pakito\python.exe"
if not exist "%CONDA%" (
  echo Couldn't find conda at %CONDA%
  echo Tell Claude where Miniconda is installed.
  pause & exit /b 1
)
if not exist "%PY%" (
  echo Creating the "pakito" Python 3.12 environment...
  "%CONDA%" create -n pakito python=3.12 -y -c conda-forge --override-channels || (pause & exit /b 1)
)
echo Installing packages...
"%PY%" -m pip install -r assistant\requirements.txt || (pause & exit /b 1)
echo.
echo Done. Now run run-pakito.bat
pause

@echo off
cd /d "%~dp0"
set "PY=%USERPROFILE%\miniconda3\envs\pakito\python.exe"
if not exist "%PY%" (
  echo Run setup-pakito.bat first.
  pause & exit /b 1
)
if /i "%1"=="text" (
  "%PY%" -m assistant
  pause & exit /b
)
if /i "%1"=="spotify" (
  "%PY%" -m assistant.spotify
  pause & exit /b
)
if /i "%1"=="youtube" (
  "%PY%" -m assistant.podcasts
  pause & exit /b
)
if "%GEMINI_API_KEY%"=="" (
  echo GEMINI_API_KEY isn't set in this window. Close it and open a new one after setx.
  pause & exit /b 1
)
if /i "%1"=="check" (
  "%PY%" -m assistant.check_live %2
  pause & exit /b
)
"%PY%" -m assistant.voice
pause

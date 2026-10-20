@echo off
cd /d "%~dp0"
set "PY=%USERPROFILE%\miniconda3\envs\pakito\python.exe"
if not exist "%PY%" set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" (
  echo Run setup-pakito.bat first.
  pause & exit /b 1
)
if /i "%1"=="setup" (
  "%PY%" -m assistant.setup_wizard
  pause & exit /b
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
if /i "%1"=="mic" (
  "%PY%" -m assistant.calls
  pause & exit /b
)
if /i "%1"=="mcp" (
  "%PY%" assistant\mcp_server.py register
  pause & exit /b
)
if /i "%1"=="pet" (
  "%PY%" -m assistant.pet demo
  exit /b
)
if /i "%1"=="calendar" (
  "%PY%" -m assistant.calendar_feed
  pause & exit /b
)
if /i "%1"=="keys" (
  "%PY%" -m assistant.voice keytest
  pause & exit /b
)
if /i "%1"=="check" (
  "%PY%" -m assistant.check_live %2
  pause & exit /b
)
"%PY%" -m assistant.voice
pause

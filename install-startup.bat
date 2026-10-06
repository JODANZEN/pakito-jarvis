@echo off
rem Starts Pakito (minimized) every time you log in to Windows.
rem   install-startup.bat          install
rem   install-startup.bat remove   uninstall
set "LNK=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\Pakito.lnk"
if /i "%1"=="remove" (
  del "%LNK%" 2>nul
  echo Removed. Pakito won't start with Windows anymore.
  pause & exit /b
)
powershell -NoProfile -Command "$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:LNK); $s.TargetPath='%~dp0run-pakito.bat'; $s.WorkingDirectory='%~dp0'; $s.WindowStyle=7; $s.Description='Pakito voice assistant'; $s.Save()" || (pause & exit /b 1)
echo Installed: Pakito starts minimized every time you log in.
echo (Undo with: install-startup.bat remove)
pause

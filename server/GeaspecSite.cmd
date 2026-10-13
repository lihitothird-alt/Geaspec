@echo off
rem Start the Geaspec booking site (closing this window stops the site)
chcp 65001 >nul
cd /d "%~dp0"
if not exist "%~dp0python\python.exe" (
  echo zip の中から直接開いています。zip を右クリックして「すべて展開」を押し、
  echo 展開したフォルダの中の GeaspecSite.cmd をダブルクリックしてください。
  pause
  exit /b 1
)
"%~dp0python\python.exe" "%~dp0site_launcher.py"
pause

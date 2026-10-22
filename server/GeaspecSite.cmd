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
set "ARGS="
:start
rem A new version downloaded by the site itself waits in "update" (ready.txt is written last)
if exist "%~dp0update\ready.txt" call :apply_update
"%~dp0python\python.exe" "%~dp0site_launcher.py" %ARGS%
rem 75 = the site stopped to switch to the new version
if "%errorlevel%"=="75" goto start
pause
exit /b

:apply_update
rem Copy the new files over this folder (GeaspecSite.cmd itself is not replaced while it is running)
echo Updating Geaspec...
set "FAILED="
for %%d in (python lib app) do (
  if exist "%~dp0update\GeaspecSite\%%d" (
    robocopy "%~dp0update\GeaspecSite\%%d" "%~dp0%%d" /MIR /R:3 /W:2 /NFL /NDL /NJH /NJS /NP >nul
    if errorlevel 8 set "FAILED=1"
  )
)
robocopy "%~dp0update\GeaspecSite" "%~dp0." *.py /R:3 /W:2 /NFL /NDL /NJH /NJS /NP >nul
if errorlevel 8 set "FAILED=1"
rem On failure the site starts as it is and downloads the new version again later
if defined FAILED (set "ARGS=--update-failed") else (set "ARGS=--after-update")
rmdir /s /q "%~dp0update"
exit /b

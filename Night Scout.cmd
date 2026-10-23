@echo off
rem Night Scout for Windows. Double-click to start it; it opens in your browser (the Setup
rem walkthrough the first time, your Digest after that). Close this window to stop it.
rem The first launch installs Night Scout into .venv here, which takes a few minutes.
setlocal EnableExtensions
cd /d "%~dp0"

if exist ".venv\Scripts\night-scout.exe" goto ollama
set "PY="
for %%V in (3.13 3.12 3.11) do if not defined PY (py -%%V -c "import sys" >nul 2>nul && set "PY=py -%%V")
if not defined PY (python -c "import sys; sys.exit(sys.version_info < (3, 11))" >nul 2>nul && set "PY=python")
if not defined PY (
  echo Night Scout needs Python 3.11 or newer: https://www.python.org/downloads/
  echo Tick "Add python.exe to PATH" in the installer, then double-click this again.
  pause
  exit /b 1
)
echo First launch: installing Night Scout into .venv ...
%PY% -m venv .venv || goto installfail
".venv\Scripts\python.exe" -m pip install -q --upgrade pip || goto installfail
".venv\Scripts\python.exe" -m pip install -q -e . || goto installfail

:ollama
rem Ollama runs Gemma on this PC. Start it if it's installed but not running.
curl -s -m 2 http://127.0.0.1:11434/api/version >nul 2>nul && goto start
if exist "%LOCALAPPDATA%\Programs\Ollama\ollama app.exe" (
  echo Starting Ollama ...
  start "" "%LOCALAPPDATA%\Programs\Ollama\ollama app.exe"
) else (
  where ollama >nul 2>nul && (echo Starting Ollama ... & start "Ollama" /min ollama serve) || echo Ollama isn't installed ^(https://ollama.com^). You can also pick a cloud model in Setup.
)
for /l %%i in (1,1,20) do (curl -s -m 1 http://127.0.0.1:11434/api/version >nul 2>nul && goto start) & timeout /t 1 /nobreak >nul

:start
".venv\Scripts\python.exe" -c "from night_scout.core.paths import default_config_path as p; print(p())" > "%TEMP%\night-scout-config.txt"
set /p CONFIG=<"%TEMP%\night-scout-config.txt"
if exist "%CONFIG%" (
  ".venv\Scripts\night-scout.exe" serve %*
) else (
  ".venv\Scripts\night-scout.exe" setup %*
)
if errorlevel 1 (
  echo Night Scout stopped with an error.
  pause
)
exit /b 0

:installfail
echo The install didn't finish (see above).
rmdir /s /q .venv 2>nul
pause
exit /b 1

@echo off
setlocal
cd /d "%~dp0"

if not exist "pyproject.toml" (
  echo This launcher must be placed in the project root directory.
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo Creating Python virtual environment...
  py -3 -m venv .venv
  if errorlevel 1 python -m venv .venv
  if errorlevel 1 (
    echo Failed to create .venv. Please install Python 3.11 or newer.
    pause
    exit /b 1
  )
)

if not exist ".venv\Scripts\zju-seat-assistant.exe" (
  echo Installing project dependencies...
  ".venv\Scripts\python.exe" -m pip install -e ".[test]"
  if errorlevel 1 (
    echo Failed to install project dependencies.
    pause
    exit /b 1
  )
)

if not exist ".venv\.playwright-chromium-installed" (
  echo Installing Playwright Chromium...
  ".venv\Scripts\python.exe" -m playwright install chromium
  if errorlevel 1 (
    echo Failed to install Playwright Chromium.
    pause
    exit /b 1
  )
  echo installed > ".venv\.playwright-chromium-installed"
)

if not exist ".venv\Scripts\zju-seat-assistant.exe" (
  echo The application command is still missing after setup.
  pause
  exit /b 1
)

powershell -NoProfile -Command "try { Invoke-RestMethod -Uri 'http://127.0.0.1:8765/api/health' -TimeoutSec 1 | Out-Null; exit 0 } catch { exit 1 }"
if not errorlevel 1 (
  echo Service is already running. Opening console...
  goto ready
)

start "ZJU Seat Assistant" /min ".venv\Scripts\zju-seat-assistant.exe"

for /l %%i in (1,1,30) do (
  powershell -NoProfile -Command "try { Invoke-RestMethod -Uri 'http://127.0.0.1:8765/api/health' -TimeoutSec 1 | Out-Null; exit 0 } catch { exit 1 }"
  if not errorlevel 1 goto ready
  timeout /t 1 /nobreak >nul
)

echo The service did not become ready.
pause
exit /b 1

:ready
start "" "http://127.0.0.1:8765"
exit /b 0

@echo off
setlocal
cd /d "%~dp0"

if not exist "pyproject.toml" (
  echo This launcher must be placed in the project root directory.
  pause
  exit /b 1
)

set "PYTHON_CMD="
where py >nul 2>nul
if not errorlevel 1 set "PYTHON_CMD=py -3"
if "%PYTHON_CMD%"=="" (
  where python >nul 2>nul
  if not errorlevel 1 set "PYTHON_CMD=python"
)

if not exist ".venv\Scripts\python.exe" (
  if "%PYTHON_CMD%"=="" (
    echo Python was not found.
    echo Please install Python 3.11 or newer from https://www.python.org/downloads/
    echo During installation, check "Add python.exe to PATH", then run this launcher again.
    pause
    exit /b 1
  )
  echo Creating Python virtual environment...
  %PYTHON_CMD% -m venv .venv
  if errorlevel 1 (
    echo Failed to create .venv. Please install Python 3.11 or newer.
    pause
    exit /b 1
  )
)

".venv\Scripts\python.exe" -m pip --version >nul 2>nul
if errorlevel 1 (
  echo pip is missing in .venv. Trying to repair it...
  ".venv\Scripts\python.exe" -m ensurepip --upgrade
  if errorlevel 1 (
    echo Failed to repair pip in .venv.
    echo Please delete the .venv folder, install Python 3.11 or newer, then run this launcher again.
    pause
    exit /b 1
  )
)

if not exist ".venv\Scripts\zju-seat-assistant.exe" (
  echo Installing project dependencies...
  ".venv\Scripts\python.exe" -m pip install --upgrade pip
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

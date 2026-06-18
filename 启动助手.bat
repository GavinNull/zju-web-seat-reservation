@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\zju-seat-assistant.exe" (
  echo The application environment is missing.
  echo Please follow README.md to install it first.
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

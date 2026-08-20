@echo off
chcp 65001 >nul
setlocal EnableExtensions EnableDelayedExpansion
set "DIR=%~dp0"
set "PORT=8090"

REM === resolve Python interpreter ===
set "PY="
if exist "%DIR%.venv\Scripts\python.exe" (
  set "PY=%DIR%.venv\Scripts\python.exe"
) else (
  where python >nul 2>nul && set "PY=python"
)
if not defined PY (
  where py >nul 2>nul && set "PY=py -3"
)
if not defined PY (
  echo [ERROR] Python not found. Please install Python 3.10+ and add it to PATH.
  pause
  exit /b 1
)

REM === ensure dependencies (auto venv + install on first run) ===
"%PY%" -c "import fastapi" >nul 2>nul || (
  echo [INFO] Dependencies missing. Creating venv and installing (first run may take a while)...
  if not exist "%DIR%.venv\Scripts\python.exe" (
    "%PY%" -m venv "%DIR%.venv" || (echo [ERROR] venv creation failed & pause & exit /b 1)
  )
  "%DIR%.venv\Scripts\python.exe" -m pip install -U pip >nul 2>nul
  "%DIR%.venv\Scripts\python.exe" -m pip install -r "%DIR%requirements.txt" || (echo [ERROR] pip install failed & pause & exit /b 1)
  set "PY=%DIR%.venv\Scripts\python.exe"
)

REM === already running? ===
powershell -NoProfile -Command "try{(Invoke-WebRequest -Uri 'http://127.0.0.1:%PORT%/api/state' -UseBasicParsing -TimeoutSec 2).StatusCode; exit 0}catch{exit 1}" >nul 2>nul
if !errorlevel!==0 (
  echo [OK] Service already running.
  goto :openbrowser
)

REM === start service in background ===
if not exist "%DIR%logs" mkdir "%DIR%logs"
echo [INFO] Starting service...
start "" /b "%PY%" "%DIR%run.py" > "%DIR%logs\console.log" 2>&1
set "READY=0"
for /l %%i in (1,1,40) do (
  powershell -NoProfile -Command "try{(Invoke-WebRequest -Uri 'http://127.0.0.1:%PORT%/api/state' -UseBasicParsing -TimeoutSec 2).StatusCode; exit 0}catch{exit 1}" >nul 2>nul
  if !errorlevel!==0 (
    set "READY=1"
    goto :openbrowser
  )
  timeout /t 1 >nul
)

:openbrowser
if "%READY%"=="1" ( echo [OK] Service started. ) else ( echo [WARN] Service may still be starting; check %DIR%logs\console.log )
echo [INFO] Opening dashboard: http://127.0.0.1:%PORT%
start "" http://127.0.0.1:%PORT%
endlocal

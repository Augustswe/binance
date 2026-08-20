@echo off
setlocal EnableExtensions
set "PORT=8090"
set "FOUND=0"

REM === find the PID listening on PORT and kill it ===
for /f "tokens=5" %%a in ('netstat -ano 2^>nul ^| findstr /i ":%PORT%" ^| findstr /i "LISTENING"') do (
  taskkill /F /PID %%a >nul 2>nul && ( echo [OK] Stopped PID %%a & set "FOUND=1" )
)

if "%FOUND%"=="0" (
  echo [INFO] No service listening on port %PORT% (not running).
)
endlocal

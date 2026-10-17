@echo off
setlocal
cd /d "%~dp0"

echo ========================================================
echo Starting Alphabot...
echo ========================================================
echo.

set "PY=%~dp0backend\.venv\Scripts\python.exe"

if not exist "%PY%" (
    echo [ERROR] Backend environment not found:
    echo         %PY%
    echo         Create backend\.venv and install backend\requirements.txt first.
    echo.
    pause
    exit /b 1
)

if not exist "%~dp0backend\diablo_canyon.db" (
    echo [ERROR] Plant databases were not found in the backend folder.
    echo         This script does not create them, because that replaces existing databases.
    echo.
    pause
    exit /b 1
)

netstat -ano | findstr /C:":8000 " | findstr "LISTENING" >nul
if %errorlevel%==0 (
    echo [INFO] Backend is already running on port 8000.
) else (
    echo [INFO] Starting Backend Server (Port 8000)...
    start "Alphabot Backend" cmd /k "cd /d ""%~dp0backend"" && ""%PY%"" -m uvicorn main:app --host 127.0.0.1 --port 8000"
)

ping -n 4 127.0.0.1 >nul

netstat -ano | findstr /C:":3000 " | findstr "LISTENING" >nul
if %errorlevel%==0 (
    echo [INFO] Frontend is already running on port 3000.
) else (
    echo [INFO] Starting Frontend Server (Port 3000)...
    start "Alphabot Frontend" cmd /k "cd /d ""%~dp0frontend"" && npm run dev"
)

echo.
echo ========================================================
echo Alphabot is starting up!
echo ========================================================
echo.
echo Frontend: http://localhost:3000
echo Backend:  http://127.0.0.1:8000
echo API Docs: http://127.0.0.1:8000/docs
echo.
echo Press any key to close this window. The servers keep running.
pause >nul

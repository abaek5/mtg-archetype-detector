@echo off
title MTG Match Assistant
color 0A

echo ============================================
echo   MTG Archetype Detector - Match Setup
echo ============================================
echo.

:: Check Python is installed
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found. Install from python.org
    pause
    exit /b 1
)

:: Set paths
set WATCHER=%~dp0watcher.py
set NGROK=%USERPROFILE%\Downloads\ngrok-v3-stable-windows-amd64\ngrok.exe

:: Check watcher exists
if not exist "%WATCHER%" (
    echo [ERROR] watcher.py not found in %~dp0
    pause
    exit /b 1
)

:: Check ngrok exists
if not exist "%NGROK%" (
    echo [ERROR] ngrok.exe not found at %NGROK%
    echo Please update NGROK path in this script.
    pause
    exit /b 1
)

echo [1/3] Starting Arena watcher...
start "MTG Watcher" cmd /k "python "%WATCHER%""
timeout /t 2 /nobreak >nul

echo [2/3] Starting ngrok tunnel...
start "ngrok" cmd /k ""%NGROK%" http 5000"
timeout /t 3 /nobreak >nul

echo [3/3] Fetching your ngrok URL...
timeout /t 2 /nobreak >nul

:: Get the ngrok URL from its local API
for /f "delims=" %%i in ('powershell -Command "(Invoke-RestMethod http://127.0.0.1:4040/api/tunnels).tunnels[0].public_url"') do set NGROK_URL=%%i

if "%NGROK_URL%"=="" (
    echo.
    echo [!] Could not auto-detect ngrok URL.
    echo     Copy it manually from the ngrok window.
    echo.
) else (
    echo.
    echo ============================================
    echo   Your ngrok URL:
    echo   %NGROK_URL%
    echo ============================================
    echo.
    echo Copying to clipboard...
    echo %NGROK_URL% | clip
    echo Done! URL is in your clipboard.
    echo.
    echo NEXT STEPS:
    echo   1. Open your Netlify site
    echo   2. Paste the URL into "Watcher URL" field
    echo   3. Click Set
    echo   4. Start Arena
    echo.
    
    :: Open Netlify site automatically
    echo Opening Netlify site...
    start "" "https://sunny-marzipan-2ef5a5.netlify.app"
)

echo ============================================
echo   Setup complete! Keep this window open.
echo ============================================
echo.
pause

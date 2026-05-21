@echo off
title MTG Match Assistant
color 0A

echo ============================================
echo   MTG Archetype Detector - Starting up...
echo ============================================
echo.

:: ── Config ────────────────────────────────────────────────────────────────────
set WATCHER=%~dp0watcher.py
set NGROK=%USERPROFILE%\Downloads\ngrok-v3-stable-windows-amd64\ngrok.exe
set NETLIFY=https://sunny-marzipan-2ef5a5.netlify.app

:: ── Checks ────────────────────────────────────────────────────────────────────
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found. Install from python.org
    pause & exit /b 1
)

if not exist "%WATCHER%" (
    echo [ERROR] watcher.py not found at: %WATCHER%
    pause & exit /b 1
)

if not exist "%NGROK%" (
    echo [ERROR] ngrok.exe not found at: %NGROK%
    pause & exit /b 1
)

:: ── Step 1: Start watcher ─────────────────────────────────────────────────────
echo [1/3] Starting Arena watcher...
start "MTG Watcher" cmd /k "cd /d "%~dp0" && python watcher.py"
timeout /t 2 /nobreak >nul

:: ── Step 2: Start ngrok ───────────────────────────────────────────────────────
echo [2/3] Starting ngrok tunnel...
start "ngrok" cmd /k ""%NGROK%" http 5000"
timeout /t 4 /nobreak >nul

:: ── Step 3: Get ngrok URL ─────────────────────────────────────────────────────
echo [3/3] Fetching ngrok URL...
for /f "delims=" %%i in ('powershell -Command ^
  "try { (Invoke-RestMethod http://127.0.0.1:4040/api/tunnels).tunnels ^| Where-Object { $_.proto -eq 'https' } ^| Select-Object -First 1 -ExpandProperty public_url } catch { '' }"' ^
) do set NGROK_URL=%%i

:: ── Step 4: Open Netlify site ─────────────────────────────────────────────────
echo.
if not "%NGROK_URL%"=="" (
    echo    ngrok URL: %NGROK_URL%
    echo    Copied to clipboard!
    echo %NGROK_URL% | clip
) else (
    echo    [!] Could not auto-detect ngrok URL.
    echo    Copy it manually from the ngrok window.
)

echo.
echo [4/4] Opening your MTG Detector...
timeout /t 1 /nobreak >nul
start "" "%NETLIFY%"

echo.
echo ============================================
echo   Ready! Keep this window open.
echo   Start Arena and begin your match.
echo ============================================
echo.
echo   The detector will auto-connect to ngrok.
echo   If it shows offline, paste the URL from
echo   the ngrok window into the Watcher URL field.
echo.
pause

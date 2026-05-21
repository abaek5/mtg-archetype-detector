@echo off
title MTG Match Assistant
color 0A

echo ============================================
echo   MTG Archetype Detector - Starting up...
echo ============================================
echo.

:: ── Config ────────────────────────────────────────────────────────────────────
set WATCHER=%~dp0watcher.py
set SITE=https://mtg-archetype-detector.pages.dev

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

:: ── Step 1: Start watcher ─────────────────────────────────────────────────────
echo [1/2] Starting Arena watcher...
start "MTG Watcher" cmd /k "cd /d "%~dp0" && python watcher.py"
timeout /t 2 /nobreak >nul

:: ── Step 2: Open site ─────────────────────────────────────────────────────────
echo [2/2] Opening MTG Detector...
:: Add timestamp to bust Cloudflare cache
for /f "tokens=2 delims==" %%i in ('wmic os get localdatetime /value') do set DT=%%i
set CACHE_KEY=%DT:~0,12%
start "" "chrome.exe" "%SITE%?v=%CACHE_KEY%" 2>nul || start "" "%SITE%?v=%CACHE_KEY%"

echo.
echo ============================================
echo   Ready! Keep the watcher window open.
echo   Start Arena and begin your match.
echo ============================================
echo.
pause

@echo off
echo Installing MTG Watcher dependencies...
pip install flask 2>nul || pip3 install flask 2>nul
echo.
echo Done! Run start-watcher.bat before your next match.
pause

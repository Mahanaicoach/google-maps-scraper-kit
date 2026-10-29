@echo off
rem Double-click to open the simple Business Finder page. Keep this window open while you use it.
cd /d "%~dp0"
docker compose up -d
start "" http://localhost:8090
python scripts\ui.py
pause

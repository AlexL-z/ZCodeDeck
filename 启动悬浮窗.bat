@echo off
rem ZCodeDeck launcher: skip if already running, install deps on first run
cd /d "%~dp0"
powershell -NoProfile -Command "try { $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 -Uri 'http://127.0.0.1:17654/health'; if ($r.Content -match 'ZCodeDeck') { exit 1 } } catch { exit 0 }"
if errorlevel 1 (
    echo ZCodeDeck is already running. Tray icon is in the system tray.
    ping -n 3 127.0.0.1 >nul
    exit /b 0
)
py -3 -c "import PySide6" >nul 2>nul
if errorlevel 1 (
    echo First run: installing PySide6, please wait...
    py -3 -m pip install PySide6
)
where pyw >nul 2>nul
if %errorlevel%==0 (
    start "" pyw -3 zcode_deck.py
) else (
    start "" pythonw zcode_deck.py
)
echo ZCodeDeck started. Tray icon is in the system tray.

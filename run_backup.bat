@echo off
REM futures_monitor G19 sqlite online hot-backup watchdog (Task Scheduler / logon / double-click)
chcp 65001 >nul
cd /d "C:\Users\Lenovo\Desktop\量化\futures_monitor"
"D:\Python\python.exe" "C:\Users\Lenovo\Desktop\量化\futures_monitor\db_backup.py" --once
if errorlevel 1 ( echo [db_backup] FAILED & pause ) else ( echo [db_backup] OK )

@echo off
cd /d "%~dp0"
REM To use a specific vault, put its path in quotes after the script name, e.g.
REM python vault_dashboard.py "path/to/your/vault"
python vault_dashboard.py
if errorlevel 1 py vault_dashboard.py
pause

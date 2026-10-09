@echo off
rem Локальный запуск Telegram Video Panel (Windows). Панель откроется на http://127.0.0.1:8765
chcp 65001 >nul
cd /d "%~dp0"
where python >nul 2>nul || (echo Нужен Python 3.9+: https://www.python.org/downloads/ & pause & exit /b 1)
if not exist .venv\Scripts\python.exe python -m venv .venv || (pause & exit /b 1)
.venv\Scripts\python -m pip install -q --upgrade pip >nul 2>nul
.venv\Scripts\python -m pip install -q -r requirements.txt || (pause & exit /b 1)
.venv\Scripts\python app.py
pause

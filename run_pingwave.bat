@echo off
cd /d "%~dp0"

REM Проверяем интерпретатор (как в pingwave)
if not exist ".venv\Scripts\pythonw.exe" (
    echo Creating virtual environment...
    python -m venv .venv
    
    REM КРИТИЧНО: pip ИЗ venv, НЕ из системы!
    echo Installing requirements...
    ".venv\Scripts\python.exe" -m pip install --upgrade pip
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
)

REM Запуск напрямую (без activate)
start "" ".venv\Scripts\pythonw.exe" main.py

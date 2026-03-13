@echo off
cd /d "%~dp0"

REM Проверяем venv (как в run_pingwave.bat)
if not exist ".venv\Scripts\pythonw.exe" (
    echo Виртуальное окружение не найдено.
    echo Сначала запусти setup_pingwave.bat
    pause
    exit /b 1
)

REM Debug: используем python.exe (консольный), не pythonw (без окна)
start "" ".venv\Scripts\python.exe" main.py

REM Debug версия НЕ exit сразу — ждёт завершения Python
REM (удаляем exit, чтобы видеть вывод ошибок)

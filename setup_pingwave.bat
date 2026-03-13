@echo off
setlocal

REM Переходим в каталог скрипта
cd /d "%~dp0"

echo === PingWave setup ===

REM Проверяем наличие python
python --version >nul 2>&1
if errorlevel 1 (
    echo Python не найден в PATH.
    echo Установи Python 3.10+ и добавь его в PATH.
    pause
    exit /b 1
)

REM Создаём/обновляем виртуальное окружение .venv
if not exist ".venv" (
    echo Создаю виртуальное окружение .venv ...
    python -m venv .venv
) else (
    echo Виртуальное окружение .venv уже существует.
)

REM Обновляем pip и ставим зависимости
echo Активирую .venv и устанавливаю зависимости...
call ".venv\Scripts\python.exe" -m pip install --upgrade pip
call ".venv\Scripts\python.exe" -m pip install -r requirements.txt

echo.
echo Установка завершена.
echo Для запуска используй run_pingwave.bat
pause
endlocal

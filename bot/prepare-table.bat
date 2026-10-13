@echo off
chcp 65001 >nul
rem Подготовка Google Таблицы: создаёт листы и печатает ссылку-приглашение для директора.
rem Запускать после того, как в .env заполнены BOT_TOKEN, STORAGE=sheets, SHEET_ID,
rem а ключ Google лежит в папке secrets под именем service-account.json.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Сначала один раз запустите start.bat — он подготовит окружение.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m greenline_bot.setup_sheet
echo.
pause

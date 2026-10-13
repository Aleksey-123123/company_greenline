@echo off
chcp 65001 >nul
rem Запуск бота на Windows: двойной клик по этому файлу.
rem Первый раз ставит библиотеки (1-3 минуты), создаёт .env и открывает его в Блокноте.
cd /d "%~dp0"

set PY=py -3
%PY% --version >nul 2>nul || set PY=python
%PY% --version >nul 2>nul || (
  echo.
  echo Python не найден. Установите его с https://www.python.org/downloads/
  echo При установке поставьте галочку "Add python.exe to PATH".
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo Первый запуск: готовлю окружение...
  %PY% -m venv .venv || (echo Не удалось создать окружение & pause & exit /b 1)
)
".venv\Scripts\python.exe" -m pip install -q --disable-pip-version-check -r requirements.txt || (
  echo Не удалось поставить библиотеки. Проверьте интернет и запустите ещё раз.
  pause
  exit /b 1
)

if not exist "secrets" mkdir secrets
if not exist ".env" (
  copy ".env.example" ".env" >nul
  echo.
  echo Создан файл .env. Сейчас он откроется в Блокноте:
  echo вставьте токен бота после BOT_TOKEN= , сохраните (Ctrl+S) и закройте Блокнот.
  notepad ".env"
)

echo.
echo Бот запускается. Пока это окно открыто — бот работает.
echo Закрыть окно = остановить бота.
echo.
".venv\Scripts\python.exe" -m greenline_bot
echo.
echo Бот остановлен.
pause

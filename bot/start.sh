#!/usr/bin/env bash
# Запуск бота на Mac/Linux: ./start.sh  (первый раз ставит библиотеки и создаёт .env)
set -e
cd "$(dirname "$0")"
command -v python3 >/dev/null || { echo "Нужен Python 3: https://www.python.org/downloads/"; exit 1; }
[ -x .venv/bin/python ] || python3 -m venv .venv
.venv/bin/python -m pip install -q --disable-pip-version-check -r requirements.txt
mkdir -p secrets
if [ ! -f .env ]; then
  cp .env.example .env
  echo "Создан файл .env — впишите токен бота после BOT_TOKEN= и запустите ./start.sh ещё раз."
  exit 0
fi
echo "Бот запускается. Ctrl+C — остановить."
exec .venv/bin/python -m greenline_bot

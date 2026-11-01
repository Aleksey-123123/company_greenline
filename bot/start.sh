#!/usr/bin/env bash
# Запуск бота на Mac/Linux: ./start.sh   (подготовка таблицы: ./prepare-table.sh)
set -e
cd "$(dirname "$0")"
command -v python3 >/dev/null || { echo "Нужен Python 3: https://www.python.org/downloads/"; exit 1; }
[ -x .venv/bin/python ] || python3 -m venv .venv
exec .venv/bin/python launcher.py "$@"

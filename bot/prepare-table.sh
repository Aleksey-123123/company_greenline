#!/usr/bin/env bash
# Подготовка Google Таблицы: листы + ссылка-приглашение для директора.
set -e
cd "$(dirname "$0")"
[ -x .venv/bin/python ] || { echo "Сначала один раз запустите ./start.sh"; exit 1; }
exec .venv/bin/python -m greenline_bot.setup_sheet

"""Создать в Google Таблице листы с заголовками (один раз, перед запуском бота).

    python -m greenline_bot.setup_sheet

Существующие листы не трогает: если лист уже есть, только проверяет заголовки.
"""

from __future__ import annotations

import json
import secrets
import sys
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

from .config import ConfigError, load_config
from .domain import parse_bool
from .storage import SCHEMA, align

EXAMPLE_OBJECTS = [["О-01", "Пример: мост через р. Иня", "НСО", "да", "", "удалите эту строку"]]


def main() -> int:
    try:
        cfg = load_config(require_token=False)
        if cfg.storage != "sheets":
            raise ConfigError("в .env должно быть STORAGE=sheets")
    except ConfigError as e:
        print(f"ОШИБКА настройки: {e}", file=sys.stderr)
        return 1
    import gspread

    book = gspread.service_account(filename=cfg.google_credentials).open_by_key(cfg.sheet_id)
    existing = {ws.title: ws for ws in book.worksheets()}
    problems = 0
    for name, headers in SCHEMA.items():
        if name in existing:
            have = existing[name].row_values(1)
            if have[:len(headers)] != headers:
                print(f"! «{name}»: заголовки отличаются.\n  нужно: {headers}\n  есть:  {have}")
                problems += 1
            else:
                print(f"= «{name}» уже есть, заголовки верные")
            continue
        ws = book.add_worksheet(title=name, rows=1000, cols=len(headers))
        ws.append_row(headers, value_input_option="RAW")
        ws.freeze(rows=1)
        if name == "Объекты":
            ws.append_rows(EXAMPLE_OBJECTS, value_input_option="RAW")
        print(f"+ создан лист «{name}»")
    if "Лист1" in existing and len(existing) == 1:
        book.del_worksheet(existing["Лист1"])
    if problems:
        print(f"Есть расхождения в заголовках: {problems}. Исправьте вручную.")
        return 1
    print("Листы готовы.")
    _director_invite(book, cfg)
    return 0


def _director_invite(book, cfg) -> None:
    """Если директора в таблице ещё нет — печатаем ссылку-приглашение для него.

    Дальше директор приглашает всех остальных кнопкой «🔗 Пригласить» в боте:
    никому не нужно узнавать свой Telegram ID и вписывать его в таблицу.
    """
    users = book.worksheet("Пользователи").get_all_records(expected_headers=SCHEMA["Пользователи"])
    if any(str(u.get("Роль", "")).strip().lower() == "директор" and parse_bool(u.get("Активен"))
           for u in users):
        print("Директор в таблице уже есть — приглашения создаются в боте кнопкой «🔗 Пригласить».")
        return
    code = secrets.token_urlsafe(9)
    now = datetime.now(ZoneInfo(cfg.tz)).isoformat(timespec="seconds")
    book.worksheet("Приглашения").append_row(
        align(book.worksheet("Приглашения").row_values(1),
              {"Код": code, "Роль": "директор", "Объекты": "все", "Создал": "setup_sheet", "Создано": now}),
        value_input_option="RAW")
    username = _bot_username(cfg.bot_token)
    if username:
        print(f"\nСсылка для директора (одноразовая, 7 дней):\nhttps://t.me/{username}?start=inv_{code}")
    else:
        print(f"\nBOT_TOKEN не задан или бот недоступен. Когда бот будет создан, директору открыть:\n"
              f"https://t.me/<имя_бота>?start=inv_{code}")


def _bot_username(token: str) -> str:
    if not token:
        return ""
    try:
        with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/getMe", timeout=10) as r:
            return json.load(r)["result"].get("username", "")
    except Exception as e:  # сеть, неверный токен
        print(f"(не удалось узнать имя бота: {type(e).__name__})")
        return ""


if __name__ == "__main__":
    sys.exit(main())

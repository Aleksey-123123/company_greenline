"""Создать в Google Таблице листы с заголовками (один раз, перед запуском бота).

    python -m greenline_bot.setup_sheet

Существующие листы не трогает: если лист уже есть, только проверяет заголовки.
"""

from __future__ import annotations

import sys

from .config import ConfigError, load_config
from .storage import SCHEMA

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
    print("Готово." if not problems else f"Есть расхождения в заголовках: {problems}. Исправьте вручную.")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())

"""Запуск: python -m greenline_bot (из папки bot/)."""

from __future__ import annotations

import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage as FSMMemory

from .config import ConfigError, load_config
from .handlers import AccessMiddleware, routers
from .storage import MemoryStorage, SheetsStorage


def build_dispatcher(db, cfg, routers=routers) -> Dispatcher:
    """routers — параметр для тестов: роутер можно подключить только к одному диспетчеру."""
    dp = Dispatcher(storage=FSMMemory(), db=db, cfg=cfg)
    access = AccessMiddleware()
    dp.message.outer_middleware(access)
    dp.callback_query.outer_middleware(access)
    dp.include_routers(*routers)
    return dp


async def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        cfg = load_config()
    except ConfigError as e:
        print(f"ОШИБКА настройки: {e}", file=sys.stderr)
        return 1
    if cfg.storage == "sheets":
        db = SheetsStorage(cfg.sheet_id, cfg.google_credentials)
    else:
        db = MemoryStorage(demo_mode=True)
        logging.warning("STORAGE=memory: данные не сохраняются, любой написавший "
                        "становится директором — только для пробы.")
    bot = Bot(cfg.bot_token)
    dp = build_dispatcher(db, cfg)
    await bot.delete_webhook(drop_pending_updates=False)
    await dp.start_polling(bot)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

"""Запуск: python -m greenline_bot (из папки bot/)."""

from __future__ import annotations

import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramNetworkError, TelegramUnauthorizedError
from aiogram.fsm.storage.memory import SimpleEventIsolation
from aiogram.fsm.storage.memory import MemoryStorage as FSMMemory

from .config import ConfigError, load_config
from .handlers import AccessMiddleware, routers
from .storage import MemoryStorage, SheetsStorage


def build_dispatcher(db, cfg, routers=routers) -> Dispatcher:
    """routers — параметр для тестов: роутер можно подключить только к одному диспетчеру."""
    # SimpleEventIsolation: события одного пользователя — строго по очереди.
    # Без этого двойное нажатие «Сохранить» успевает записать две строки.
    dp = Dispatcher(storage=FSMMemory(), events_isolation=SimpleEventIsolation(), db=db, cfg=cfg)
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
    if cfg.archive_chat_id and not str(cfg.archive_chat_id).startswith("-100"):
        logging.warning("ARCHIVE_CHAT_ID=%s не начинается с -100: ссылки на фото не откроются. "
                        "Используйте закрытый канал (см. bot/README.md).", cfg.archive_chat_id)
    if cfg.storage == "sheets":
        db = SheetsStorage(cfg.sheet_id, cfg.google_credentials)
    else:
        db = MemoryStorage(demo_mode=True)
        logging.warning("STORAGE=memory: данные не сохраняются, любой написавший "
                        "становится директором — только для пробы.")
    session = AiohttpSession(proxy=cfg.telegram_proxy) if cfg.telegram_proxy else None
    bot = Bot(cfg.bot_token, session=session)
    dp = build_dispatcher(db, cfg)
    try:
        me = await bot.get_me()
        await bot.delete_webhook(drop_pending_updates=False)
    except TelegramUnauthorizedError:
        print("\nОШИБКА: Telegram не принял токен. Скопируйте его заново из @BotFather и вставьте "
              "в .env после BOT_TOKEN= без пробелов.", file=sys.stderr)
        await bot.session.close()
        return 1
    except TelegramNetworkError as e:
        print(NO_TELEGRAM.format(error=e, proxy=cfg.telegram_proxy or "не задан"), file=sys.stderr)
        await bot.session.close()
        return 2
    print(f"\nБот @{me.username} на связи. Откройте его в Telegram и нажмите «Запустить».\n", flush=True)
    await dp.start_polling(bot)
    return 0


NO_TELEGRAM = """
ОШИБКА: компьютер не может соединиться с сервером Telegram для ботов (api.telegram.org).
Подробности: {error}
Прокси: {proxy}

Обычно это фильтр у провайдера или в офисной сети. Приложение Telegram при этом может
работать — у него свои способы обхода, у ботов их нет.

Что сделать:
1. Проверить: открыть в браузере https://api.telegram.org — если страница не открывается,
   дело в сети этого компьютера.
2. Если есть VPN — включить его на этом компьютере и снова запустить start.bat.
3. Если есть прокси — вписать в .env строку TELEGRAM_PROXY=socks5://логин:пароль@адрес:порт
   (или http://…) и снова запустить start.bat.
4. Надёжнее — запустить бота на сервере, где Telegram доступен (см. ЗАПУСК-НА-КОМПЬЮТЕРЕ.md).
"""


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

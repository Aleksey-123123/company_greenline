"""Настройки из переменных окружения (или файла .env рядом с bot/)."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path


def _load_dotenv(path: Path) -> None:
    """Минимальный разбор .env: KEY=VALUE, # комментарии. Окружение важнее файла."""
    if not path.exists():
        return
    # utf-8-sig: Блокнот Windows может сохранить файл с невидимой меткой BOM в начале
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass(frozen=True)
class Config:
    bot_token: str
    storage: str
    sheet_id: str
    google_credentials: str
    archive_chat_id: int | None
    admin_ids: frozenset[int]
    tz: str


class ConfigError(Exception):
    pass


def sheet_id_from(value: str) -> str:
    """ID таблицы из полного адреса: можно вставить ссылку из браузера как есть."""
    value = value.strip()
    m = re.search(r"/spreadsheets/d/([A-Za-z0-9_-]+)", value)
    return m.group(1) if m else value


BOT_DIR = Path(__file__).resolve().parent.parent


def load_config(env_file: Path | None = None, require_token: bool = True) -> Config:
    """require_token=False — для setup_sheet: таблицу можно подготовить до бота."""
    _load_dotenv(env_file or BOT_DIR / ".env")
    token = os.environ.get("BOT_TOKEN", "").strip()
    if require_token and not token:
        raise ConfigError("BOT_TOKEN не задан: откройте файл .env в папке bot и вставьте токен "
                          "от @BotFather после BOT_TOKEN=")
    storage = os.environ.get("STORAGE", "sheets").strip().lower()
    if storage not in {"sheets", "memory"}:
        raise ConfigError("STORAGE должен быть sheets или memory")
    sheet_id = sheet_id_from(os.environ.get("SHEET_ID", ""))
    creds = os.environ.get("GOOGLE_CREDENTIALS", "").strip()
    if creds and not Path(creds).is_absolute():
        creds = str(BOT_DIR / creds)          # относительный путь — от папки bot/
    if storage == "sheets" and not (sheet_id and creds):
        raise ConfigError("Для STORAGE=sheets нужны SHEET_ID и GOOGLE_CREDENTIALS")
    archive = os.environ.get("ARCHIVE_CHAT_ID", "").strip()
    try:
        admins = frozenset(int(x) for x in os.environ.get("ADMIN_IDS", "").replace(" ", "").split(",") if x)
        archive_id = int(archive) if archive else None
    except ValueError as e:
        raise ConfigError(f"ADMIN_IDS и ARCHIVE_CHAT_ID — числа: {e}")
    return Config(bot_token=token, storage=storage, sheet_id=sheet_id,
                  google_credentials=creds, archive_chat_id=archive_id,
                  admin_ids=admins, tz=os.environ.get("TZ", "Asia/Novosibirsk").strip())

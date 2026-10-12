"""Хранилище: схема листов таблицы и две реализации — в памяти и Google Таблица.

Схема одна для обеих реализаций. Колонка «Битрикс ID» в листах с данными
пока пустая: её заполнит связка с Битрикс24 (notes/0023, этап 3).

Запись идёт по заголовкам листа, а не по номерам колонок: помощник может
добавлять свои колонки в любом месте. Переименовывать колонки из SCHEMA нельзя.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Protocol

from .domain import (DELIVERY_FULL, REQUEST_CLOSED_STATUSES, SiteObject, User,
                     parse_bool, parse_object_list)

log = logging.getLogger(__name__)

# Лист → обязательные колонки. В новом листе они создаются в этом порядке.
SCHEMA: dict[str, list[str]] = {
    "Пользователи": ["Telegram ID", "ФИО", "Роль", "Объекты", "Активен",
                     "Username", "Добавлен", "Комментарий"],
    "Объекты": ["ID", "Название", "Адрес", "Активен", "Битрикс ID", "Комментарий"],
    "Отчёты": ["ID", "Дата", "Время", "Объект ID", "Объект", "Автор ID", "Автор",
               "Роль", "Людей", "Бригада / подрядчик", "Техника (как написано)",
               "Машино-часы", "Сделано", "Проблемы", "Фото, шт", "Фото (ссылки)",
               "Файлы (file_id)", "Битрикс ID"],
    "Заявки": ["ID", "Создана", "Объект ID", "Объект", "Автор ID", "Автор", "Роль",
               "Тип", "Что нужно", "Нужно к", "Срочно", "Этап", "Файлы (ссылки)",
               "Файлы (file_id)", "Статус", "Поставка", "Битрикс ID",
               "Комментарий офиса"],
    "Приёмка": ["ID", "Дата", "Объект ID", "Объект", "Автор ID", "Автор",
                "Заявка ID", "Что принято", "Полностью / частично", "Комментарий",
                "Фото (ссылки)", "Файлы (file_id)", "Битрикс ID"],
    "Записи": ["ID", "Дата", "Объект ID", "Объект", "Автор ID", "Автор", "Роль",
               "Тип", "Текст", "Фото (ссылки)", "Файлы (file_id)", "Битрикс ID"],
    "Ошибки": ["ID", "Дата", "Автор ID", "Автор", "Текст", "Статус"],
}


class StorageUnavailable(Exception):
    """Таблица недоступна или сломаны заголовки — показываем пользователю."""


class Storage(Protocol):
    async def list_users(self) -> list[User]: ...
    async def list_objects(self) -> list[SiteObject]: ...
    async def add_pending_user(self, telegram_id: int, name: str, username: str,
                               added: str) -> None: ...
    async def append(self, sheet: str, row: dict) -> None: ...
    async def exists(self, sheet: str, row_id: str) -> bool: ...
    async def open_requests(self, object_id: str) -> list[dict]: ...
    async def set_request_delivery(self, request_id: str, value: str) -> bool: ...


def check_row(sheet: str, row: dict) -> None:
    unknown = set(row) - set(SCHEMA[sheet])
    if unknown:
        raise KeyError(f"{sheet}: нет колонок {sorted(unknown)}")


def align(headers: list[str], row: dict) -> list:
    """Значения строки в порядке фактических заголовков листа.

    Чужие колонки остаются пустыми. Нет обязательной колонки — ошибка: лучше
    упасть, чем молча записать данные не туда.
    """
    missing = [h for h in row if h not in headers]
    if missing:
        raise StorageUnavailable(f"в листе нет колонок {missing} — их переименовали или удалили")
    return [row.get(h, "") for h in headers]


def _user_from_record(r: dict) -> User | None:
    try:
        tid = int(str(r.get("Telegram ID", "")).strip())
    except ValueError:
        return None
    objs, all_flag = parse_object_list(r.get("Объекты", ""))
    return User(telegram_id=tid, name=str(r.get("ФИО", "")).strip(),
                role=str(r.get("Роль", "")).strip().lower(),
                active=parse_bool(r.get("Активен", "")),
                objects=objs, all_objects=all_flag)


def _object_from_record(r: dict) -> SiteObject | None:
    oid = str(r.get("ID", "")).strip()
    if not oid:
        return None
    active_raw = str(r.get("Активен", "")).strip()
    # Пустая ячейка «Активен» = объект активен: меньше работы помощнику.
    return SiteObject(id=oid, name=str(r.get("Название", "")).strip() or oid,
                      active=(active_raw == "" or parse_bool(active_raw)))


def _is_open_request(r: dict, object_id: str) -> bool:
    return (str(r.get("Объект ID", "")) == object_id
            and str(r.get("Статус", "")).strip().lower() not in REQUEST_CLOSED_STATUSES
            and str(r.get("Поставка", "")).strip().lower() != DELIVERY_FULL)


# ── В памяти: для тестов и пробы без Google ───────────────────────────────────

DEMO_OBJECTS = [("О-01", "Мост через р. Иня (демо)"), ("О-02", "Подпорная стена (демо)")]


class MemoryStorage:
    def __init__(self, demo_mode: bool = False) -> None:
        self.sheets: dict[str, list[dict]] = {name: [] for name in SCHEMA}
        # demo_mode: новый пользователь сразу становится активным директором,
        # есть два демо-объекта. Только для пробы бота без Google.
        self.demo_mode = demo_mode
        if demo_mode:
            for oid, name in DEMO_OBJECTS:
                self.sheets["Объекты"].append({"ID": oid, "Название": name, "Активен": "да"})

    async def list_users(self) -> list[User]:
        return [u for u in map(_user_from_record, self.sheets["Пользователи"]) if u]

    async def list_objects(self) -> list[SiteObject]:
        return [o for o in map(_object_from_record, self.sheets["Объекты"]) if o]

    async def add_pending_user(self, telegram_id, name, username, added) -> None:
        if any(str(r.get("Telegram ID")) == str(telegram_id)
               for r in self.sheets["Пользователи"]):
            return
        await self.append("Пользователи", {"Telegram ID": telegram_id, "ФИО": name,
                                           "Роль": "директор" if self.demo_mode else "",
                                           "Объекты": "все" if self.demo_mode else "",
                                           "Активен": "да" if self.demo_mode else "нет",
                                           "Username": username, "Добавлен": added})

    async def append(self, sheet: str, row: dict) -> None:
        check_row(sheet, row)
        self.sheets[sheet].append(dict(zip(SCHEMA[sheet], align(SCHEMA[sheet], row))))

    async def exists(self, sheet: str, row_id: str) -> bool:
        return any(r.get("ID") == row_id for r in self.sheets[sheet])

    async def open_requests(self, object_id: str) -> list[dict]:
        return [r for r in self.sheets["Заявки"] if _is_open_request(r, object_id)]

    async def set_request_delivery(self, request_id: str, value: str) -> bool:
        for r in self.sheets["Заявки"]:
            if r.get("ID") == request_id:
                r["Поставка"] = value
                return True
        return False


# ── Google Таблица ────────────────────────────────────────────────────────────

class SheetsStorage:
    """gspread синхронный — вызовы уходят в поток, чтобы не блокировать бота.

    Пользователи и объекты кэшируются на CACHE_SECONDS: помощник правит таблицу
    руками, изменения подхватываются в течение минуты. Если Google недоступен,
    отдаём последний удачный кэш — бот продолжает пускать людей.
    """

    CACHE_SECONDS = 60

    def __init__(self, sheet_id: str, credentials_path: str) -> None:
        import gspread  # импорт здесь: тесты и режим memory работают без gspread

        self._gc = gspread.service_account(filename=credentials_path)
        self._book = self._gc.open_by_key(sheet_id)
        self._ws: dict[str, object] = {}
        self._cache: dict[str, tuple[float, list[dict]]] = {}
        self._lock = asyncio.Lock()

    def _sheet(self, name: str):
        if name not in self._ws:
            self._ws[name] = self._book.worksheet(name)
        return self._ws[name]

    def _records(self, name: str) -> list[dict]:
        return self._sheet(name).get_all_records(expected_headers=SCHEMA[name],
                                                 default_blank="")

    async def _call(self, fn, *args, **kwargs):
        try:
            return await asyncio.to_thread(fn, *args, **kwargs)
        except StorageUnavailable:
            raise
        except Exception as e:
            log.exception("Google Таблица: ошибка")
            raise StorageUnavailable(str(e)) from e

    async def _cached(self, name: str) -> list[dict]:
        hit = self._cache.get(name)
        if hit and time.monotonic() - hit[0] < self.CACHE_SECONDS:
            return hit[1]
        try:
            records = await self._call(self._records, name)
        except StorageUnavailable:
            if hit:
                log.warning("«%s»: таблица недоступна, отдаю устаревший кэш", name)
                return hit[1]
            raise
        self._cache[name] = (time.monotonic(), records)
        return records

    async def list_users(self) -> list[User]:
        return [u for u in map(_user_from_record, await self._cached("Пользователи")) if u]

    async def list_objects(self) -> list[SiteObject]:
        return [o for o in map(_object_from_record, await self._cached("Объекты")) if o]

    async def add_pending_user(self, telegram_id, name, username, added) -> None:
        async with self._lock:
            records = await self._call(self._records, "Пользователи")
            if any(str(r.get("Telegram ID")) == str(telegram_id) for r in records):
                return
            await self.append("Пользователи", {"Telegram ID": telegram_id, "ФИО": name,
                                               "Активен": "нет", "Username": username,
                                               "Добавлен": added})

    def _append_sync(self, sheet: str, row: dict) -> None:
        ws = self._sheet(sheet)
        values = align(ws.row_values(1), row)
        # RAW: текст не превращается в формулы и даты, числа остаются числами.
        ws.append_row(values, value_input_option="RAW")

    async def append(self, sheet: str, row: dict) -> None:
        check_row(sheet, row)
        await self._call(self._append_sync, sheet, row)
        self._cache.pop(sheet, None)

    def _find_sync(self, sheet: str, row_id: str):
        ws = self._sheet(sheet)
        headers = ws.row_values(1)
        cell = ws.find(row_id, in_column=headers.index("ID") + 1)
        return ws, headers, cell

    async def exists(self, sheet: str, row_id: str) -> bool:
        _, _, cell = await self._call(self._find_sync, sheet, row_id)
        return cell is not None

    async def open_requests(self, object_id: str) -> list[dict]:
        records = await self._call(self._records, "Заявки")
        return [r for r in records if _is_open_request(r, object_id)]

    def _set_delivery_sync(self, request_id: str, value: str) -> bool:
        ws, headers, cell = self._find_sync("Заявки", request_id)
        if cell is None:
            return False
        ws.update_cell(cell.row, headers.index("Поставка") + 1, value)
        return True

    async def set_request_delivery(self, request_id: str, value: str) -> bool:
        return await self._call(self._set_delivery_sync, request_id, value)

"""Доменная логика без Telegram и Google: роли, доступ к объектам, разбор ввода.

Здесь всё, что можно проверить юнит-тестами без сети.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

# ── Роли ──────────────────────────────────────────────────────────────────────
# Роль пишется в таблицу «Пользователи» словом из этого списка (регистр не важен).
ROLES = {
    "прораб", "бригадир", "рп", "директор", "помощник",
    "снабжение", "экономист", "бухгалтер", "геодезист",
}
# Видят все объекты, даже если в колонке «Объекты» пусто.
ALL_OBJECTS_ROLES = {"директор", "помощник", "снабжение", "экономист", "бухгалтер"}
# Кому приходит уведомление о новой заявке (если объект им доступен).
NOTIFY_REQUEST_ROLES = {"директор", "помощник", "снабжение", "рп"}
# Кому приходит уведомление о проблеме / допработе / «ждём от заказчика».
NOTIFY_NOTE_ROLES = {"директор", "помощник", "рп"}

TRUE_WORDS = {"да", "yes", "y", "1", "true", "+", "активен", "вкл"}
ALL_WORDS = {"все", "всё", "all", "*"}

# Списки значений — те же, что в Битриксе (notes/0020, шаг 3.3), чтобы потом
# перенос шёл один в один.
REQUEST_TYPES = ["материал", "подрядчик", "техника", "прочее"]
NOTE_TYPES = ["проблема", "допработа", "ждём от заказчика", "замечание", "прочее"]
NOTIFY_NOTE_TYPES = {"проблема", "допработа", "ждём от заказчика"}
# Статусы заявки = стадии смарт-процесса «Заявки на закуп» (notes/0010 §4).
REQUEST_STATUS_NEW = "Новая"
REQUEST_CLOSED_STATUSES = {"закрыто", "отклонено"}
DELIVERY_FULL = "получено"
DELIVERY_PART = "частично"


@dataclass
class User:
    telegram_id: int
    name: str
    role: str
    active: bool
    objects: set[str] = field(default_factory=set)   # ID объектов
    all_objects: bool = False                         # «все» в колонке «Объекты»

    @property
    def sees_all(self) -> bool:
        return self.all_objects or self.role in ALL_OBJECTS_ROLES


@dataclass
class SiteObject:
    id: str
    name: str
    active: bool = True


def parse_bool(value) -> bool:
    return str(value).strip().lower() in TRUE_WORDS


def parse_object_list(value) -> tuple[set[str], bool]:
    """«О-01, О-03» → ({'О-01','О-03'}, False); «все» → (set(), True)."""
    text = str(value or "").strip()
    if text.lower() in ALL_WORDS:
        return set(), True
    parts = [p.strip() for p in re.split(r"[,;\n]+", text)]
    return {p for p in parts if p}, False


def objects_for(user: User, objects: list[SiteObject]) -> list[SiteObject]:
    """Активные объекты, доступные пользователю, в порядке таблицы."""
    active = [o for o in objects if o.active]
    if user.sees_all:
        return active
    return [o for o in active if o.id in user.objects]


def users_to_notify(users: list[User], roles: set[str], object_id: str,
                    exclude_id: int | None = None) -> list[User]:
    """Активные пользователи нужных ролей, которым доступен объект."""
    out = []
    for u in users:
        if not u.active or u.role not in roles or u.telegram_id == exclude_id:
            continue
        if u.sees_all or object_id in u.objects:
            out.append(u)
    return out


# ── Разбор ввода ──────────────────────────────────────────────────────────────

class InputError(ValueError):
    """Ввод не распознан — текст ошибки показывается пользователю."""


NO_WORDS = {"нет", "не было", "-", "—", "0", "no", "без техники"}


def parse_people(text: str) -> int:
    m = re.fullmatch(r"\s*(\d{1,3})\s*(чел\.?|человек)?\s*", text or "")
    if not m:
        raise InputError("Напишите число людей, например: 6")
    n = int(m.group(1))
    if n > 300:
        raise InputError("Больше 300 человек? Проверьте число.")
    return n


_MACHINE_RE = re.compile(r"([^\d,;\n]+?)\s*[-–—:]?\s*(\d+(?:[.,]\d+)?)\s*(?:ч|час[а-я]*|м/?ч)?\.?(?=\s*(?:[,;\n]|$))",
                         re.IGNORECASE)


def parse_machinery(text: str) -> tuple[list[tuple[str, float]], float]:
    """«экскаватор 8, кран 4,5 ч» → ([('экскаватор', 8.0), ('кран', 4.5)], 12.5).

    «нет» → ([], 0). Если часы не найдены ни у одной позиции — ошибка: тогда
    экономист не сможет посчитать машино-часы.
    """
    t = (text or "").strip()
    if t.lower() in NO_WORDS:
        return [], 0.0
    items = []
    for m in _MACHINE_RE.finditer(t):
        name = m.group(1).strip(" ,;-–—:")
        hours = float(m.group(2).replace(",", "."))
        if name:
            items.append((name, hours))
    if not items:
        raise InputError("Не понял часы. Пример: «экскаватор 8, кран 4». "
                         "Если техники не было — «нет».")
    if any(h > 24 for _, h in items):
        raise InputError("Больше 24 часов на одну единицу за день? Проверьте.")
    return items, round(sum(h for _, h in items), 2)


def parse_due_date(text: str, today: date) -> date:
    """«15.10», «15.10.2026», «15.10.26» → дата; без года — ближайшая будущая."""
    m = re.fullmatch(r"\s*(\d{1,2})[./-](\d{1,2})(?:[./-](\d{2}|\d{4}))?\s*", text or "")
    if not m:
        raise InputError("Напишите дату так: 15.10 или 15.10.2026")
    d, mth = int(m.group(1)), int(m.group(2))
    y = m.group(3)
    try:
        if y:
            year = int(y) + (2000 if len(y) == 2 else 0)
            result = date(year, mth, d)
        else:
            result = date(today.year, mth, d)
            if result < today:
                result = date(today.year + 1, mth, d)
    except ValueError:
        raise InputError("Такой даты нет. Пример: 15.10")
    if result < today:
        raise InputError("Дата в прошлом. Укажите будущую дату.")
    if result > today + timedelta(days=366):
        raise InputError("Больше года вперёд? Проверьте дату.")
    return result


def new_id(prefix: str, now: datetime) -> str:
    """Короткий читаемый ID: «З-261012-1432-a7»."""
    return f"{prefix}-{now:%y%m%d-%H%M}-{secrets.token_hex(1)}"


def archive_link(chat_id: int, message_id: int) -> str:
    """Ссылка на сообщение в закрытой группе/канале (-100…)."""
    s = str(chat_id)
    internal = s[4:] if s.startswith("-100") else s.lstrip("-")
    return f"https://t.me/c/{internal}/{message_id}"


def safe_cell(text: str) -> str:
    """Защита от формул при выгрузке таблицы в CSV/Excel: «=…», «+…», «@…» экранируем.

    В саму Google Таблицу пишем в режиме RAW, там формулы и так не выполняются.
    """
    if text and text[0] in "=+@":
        return "'" + text
    return text

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
# Кто отправляет данные по объекту. Остальные роли только получают уведомления
# и сообщают об ошибках в боте.
SENDER_ROLES = {"прораб", "бригадир", "рп", "директор", "помощник"}

TRUE_WORDS = {"да", "yes", "y", "1", "true", "+", "активен", "вкл"}
ALL_WORDS = {"все", "всё", "all", "*"}

# Тип заявки и «Поставка» — те же значения, что поля смарт-процесса «Заявки на
# закуп» (notes/0020, шаг 3.3); статус «Новая» — его первая стадия (шаг 3.2).
# Типы записей — под шаблоны задач «Допработа» и «Ждём от заказчика» (0020,
# правки 2 и 5).
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


# Одна позиция: «название [-:] часы [ч|час…]», затем разделитель «,» «;» «и» или конец.
_MACHINE_ITEM = re.compile(
    r"\s*([^\d,;\n]+?)\s*[-–—:]?\s*(\d+(?:[.,]\d+)?)\s*(?:ч\.?|час[а-я]*|м/?ч\.?)?"
    r"\s*(?:[,;\n]|\sи\s|$)", re.IGNORECASE)
_AMBIGUOUS_NAME = re.compile(r"(^|\s)(и|по|х|x)(\s|$)", re.IGNORECASE)


def parse_machinery(text: str) -> tuple[list[tuple[str, float]], float]:
    """«экскаватор 8, кран 4,5 ч» → ([('экскаватор', 8.0), ('кран', 4.5)], 12.5).

    «нет» → ([], 0). Строка должна разбираться ЦЕЛИКОМ: если хоть кусок не
    распознан («2 экскаватора по 8 ч», «экскаватор 8ч кран 4ч» без запятой) —
    ошибка и переспрос. Иначе в «Машино-часы» молча уйдёт заниженная цифра.
    """
    t = (text or "").strip()
    if t.lower() in NO_WORDS:
        return [], 0.0
    hint = ("Не понял часы. Пишите через запятую: «экскаватор 8, кран 4». "
            "Две одинаковые машины — отдельно: «экскаватор 8, экскаватор 8». "
            "Если техники не было — «нет».")
    items, pos = [], 0
    while pos < len(t):
        m = _MACHINE_ITEM.match(t, pos)
        if not m or m.end() == pos:
            raise InputError(hint)
        name = m.group(1).strip(" ,;-–—:")
        if not name or _AMBIGUOUS_NAME.search(name):
            raise InputError(hint)
        items.append((name, float(m.group(2).replace(",", "."))))
        pos = m.end()
    if not items:
        raise InputError(hint)
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
    if not due_date_ok(result, today):
        raise InputError("Дата в прошлом или больше года вперёд. Проверьте.")
    return result


def new_id(prefix: str, now: datetime) -> str:
    """Короткий читаемый ID: «ЗК-261012-1432-a7f3c1»."""
    return f"{prefix}-{now:%y%m%d-%H%M}-{secrets.token_hex(3)}"


def due_date_ok(value: date, today: date) -> bool:
    return today <= value <= today + timedelta(days=366)


def archive_link(chat_id: int, message_id: int) -> str:
    """Ссылка на сообщение в закрытой группе/канале (-100…)."""
    s = str(chat_id)
    internal = s[4:] if s.startswith("-100") else s.lstrip("-")
    return f"https://t.me/c/{internal}/{message_id}"

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
# Счёт на оплату может приложить и снабжение.
INVOICE_ROLES = SENDER_ROLES | {"снабжение"}
# Выдают наличные под отчёт.
CASH_ISSUER_ROLES = {"директор", "помощник", "бухгалтер"}
# Кому выдают под отчёт (список в кнопках).
CASH_RECEIVER_ROLES = {"прораб", "бригадир", "рп", "снабжение", "помощник"}
# Счета: согласует директор, оплачивает бухгалтер или директор.
APPROVE_ROLES = {"директор"}
PAY_ROLES = {"директор", "бухгалтер"}
NOTIFY_INVOICE_ROLES = {"директор", "помощник", "бухгалтер"}
# Кто создаёт ссылки-приглашения.
INVITE_ROLES = {"директор", "помощник"}

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
EXPENSE_ARTICLES = ["люди", "техника", "материал", "услуга", "доставка", "прочее"]
PAY_METHODS = ["наличные", "безнал", "карта"]
INVOICE_NEW, INVOICE_APPROVED, INVOICE_PAID, INVOICE_REJECTED = (
    "на согласовании", "согласован", "оплачен", "отклонён")
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


# ── Техника ───────────────────────────────────────────────────────────────────
# Форматы взяты из реального рабочего чата прораба (notes/0024): он пишет так,
# и бот должен это принимать, а не переучивать человека.
#   «Экскаватор погрузчик 9,5 часов по 3500 = 33250»
#   «4 часа гидромолот по 4000» · «Каток 10 часов 35000» · «5 хово по 10000»
#   «Хово 1 рейс 13000» · «Экскаватор погрузчик 3500х8=28000» · «экскаватор 8, кран 4»

_NUM = r"\d+(?:[.,]\d+)?"
_UNIT = r"(?:час(?:а|ов)?|рейс(?:а|ов)?|м/?ч|ч\.?)(?![а-яё])"   # длинные формы раньше «ч»
_RATE = rf"(?:(?:по|х|x|\*|×)\s*(?P<rate>{_NUM}))"
_SUM = rf"(?:=?\s*(?P<sum>{_NUM}))"
# A: «название кол-во [ед.] [доп. название] [по ставке] [= сумма]»
_ITEM_A = re.compile(rf"^(?P<name>[^\d=]+?)\s*[-–—:]?\s*(?P<qty>{_NUM})\s*(?P<unit>{_UNIT})?"
                     rf"(?:\s+(?!(?:по|х|x)\s)(?P<name2>[^\d=]+?))??\s*{_RATE}?\s*{_SUM}?$", re.IGNORECASE)
# B: «кол-во [ед.] название [по ставке] [= сумма]»
_ITEM_B = re.compile(rf"^(?P<qty>{_NUM})\s*(?P<unit>{_UNIT})?\s*(?P<name>[^\d=]+?)"
                     rf"\s*{_RATE}?\s*{_SUM}?$", re.IGNORECASE)
_SPLIT = re.compile(r"\n|;|,(?=\s*\D)|\s+и\s+", re.IGNORECASE)
_PAY_WORDS = re.compile(r"\s*(?:нал(?:ичка|ичные)?|наличкой|б/?н|безнал|руб\.?|р\.?|₽)\s*\.?\s*$", re.IGNORECASE)
_TRIP_NAMES = re.compile(r"хово|howo|камаз|самосвал|шахман|рейс", re.IGNORECASE)
_BAD_NAME = re.compile(r"(^|\s)(и|по|х|x)(\s|$)|^(час\w*|рейс\w*|ч)$", re.IGNORECASE)


@dataclass
class Machine:
    name: str
    qty: float            # часы или рейсы
    unit: str             # «ч» или «рейс»
    rate: float | None    # ₽ за час / рейс
    cost: float | None    # ₽ за день, если известно

    def __str__(self) -> str:
        s = f"{self.name}: {self.qty:g} {self.unit}"
        if self.rate:
            s += f" × {self.rate:g}"
        if self.cost is not None:
            s += f" = {self.cost:g} ₽"
        return s


@dataclass
class MachineryReport:
    items: list[Machine]

    @property
    def hours(self) -> float:
        return round(sum(m.qty for m in self.items if m.unit == "ч"), 2)

    @property
    def trips(self) -> float:
        return round(sum(m.qty for m in self.items if m.unit == "рейс"), 2)

    @property
    def cost(self) -> float:
        return round(sum(m.cost or 0 for m in self.items), 2)

    def __str__(self) -> str:
        return "; ".join(map(str, self.items))


def _num(x: str | None) -> float | None:
    return float(x.replace(",", ".")) if x else None


MACHINERY_HINT = ("Не понял. Пишите каждую машину через запятую или с новой строки:\n"
                  "«Экскаватор 9 ч по 3500, Каток 10 ч по 3500, Хово 5 рейсов по 10000».\n"
                  "Если техники не было — «нет».")


def parse_machinery(text: str) -> MachineryReport:
    """Строка о технике → позиции с часами/рейсами и деньгами.

    Строка должна разбираться ЦЕЛИКОМ, иначе — ошибка и переспрос: молча
    записать заниженные машино-часы хуже, чем переспросить. Сумма, не
    совпавшая с «часы × ставка», принимается, только если это итог всех
    позиций выше (прораб часто пишет «= 37000» в конце двух строк).
    """
    t = (text or "").strip()
    if t.lower() in NO_WORDS:
        return MachineryReport([])
    items: list[Machine] = []
    for raw in _SPLIT.split(t):
        seg = _PAY_WORDS.sub("", raw.strip(" .,"))
        if not seg:
            continue
        m = _ITEM_A.match(seg) or _ITEM_B.match(seg)
        if not m:
            raise InputError(MACHINERY_HINT)
        name = " ".join(filter(None, [m.group("name").strip(" -–—:"),
                                      (m.groupdict().get("name2") or "").strip()]))
        if not name or _BAD_NAME.search(name):
            raise InputError(MACHINERY_HINT)
        qty, rate, total = _num(m.group("qty")), _num(m.group("rate")), _num(m.group("sum"))
        unit_raw = (m.group("unit") or "").lower()
        unit = "рейс" if unit_raw.startswith("рейс") or (not unit_raw and _TRIP_NAMES.search(name)) else "ч"
        if rate is not None and qty > rate:          # «3500х8» — ставка и часы наоборот
            qty, rate = rate, qty
        if unit == "рейс" and not unit_raw and rate is None and total is None and qty >= 1000:
            qty, total = 1, qty                      # «КамАЗ 11000» — один рейс за 11000
        limit = 24 if unit == "ч" else 50
        if qty <= 0 or qty > limit:
            raise InputError(f"«{seg}»: {qty:g} {unit} за день? Напишите количество и цену, "
                             f"например «{name} 8 ч по 3500».")
        cost = qty * rate if rate is not None else total
        if rate is not None and total is not None and abs(qty * rate - total) > 1:
            running = sum(i.cost or 0 for i in items) + qty * rate
            if abs(running - total) > 1:
                raise InputError(f"«{seg}»: {qty:g} × {rate:g} = {qty * rate:g}, а написано {total:g}. "
                                 "Проверьте.")
        items.append(Machine(name, qty, unit, rate, round(cost, 2) if cost is not None else None))
    if not items:
        raise InputError(MACHINERY_HINT)
    return MachineryReport(items)


# ── Деньги и количество ───────────────────────────────────────────────────────

_PAY_DETECT = [("наличные", re.compile(r"\bнал\w*|\bналичк\w*", re.IGNORECASE)),
               ("безнал", re.compile(r"безнал\w*|\bб/?н\b", re.IGNORECASE)),
               ("карта", re.compile(r"\bкарт\w*", re.IGNORECASE))]
_AMOUNT_TAIL = re.compile(r"(?:=\s*)?(\d[\d\s]*(?:[.,]\d{1,2})?)\s*(?:₽|р\.?|руб\.?)?\s*$", re.IGNORECASE)
_AMOUNT_HEAD = re.compile(r"^(\d[\d\s]*(?:[.,]\d{1,2})?)\s*(?:₽|р\.?|руб\.?)?\s+(\D.*)$", re.IGNORECASE)
_UNPAID = re.compile(r"\(?\s*не\s*опла[тч]\w*\s*\)?", re.IGNORECASE)


@dataclass
class Amount:
    what: str
    amount: float
    method: str | None       # распознанный способ оплаты, если был в тексте
    unpaid: bool             # «(не оплатил)» в тексте


def parse_amount(text: str) -> Amount:
    """«Заборная сетка 75 м 9900 нал» → что «Заборная сетка 75 м», 9900, наличные.

    Сумма — последнее число в строке (так пишет прораб). Слова «нал», «бн»,
    «карта», «(не оплатил)» распознаются и убираются из описания.
    """
    t = " ".join((text or "").split())
    unpaid = bool(_UNPAID.search(t))
    t = _UNPAID.sub(" ", t)
    method = None
    for name, rx in _PAY_DETECT:
        if rx.search(t):
            method = name
            t = rx.sub(" ", t)
            break
    t = " ".join(t.split()).strip(" .,-—")
    m = _AMOUNT_TAIL.search(t)
    if m:
        amount_raw, what = m.group(1), t[:m.start()]
    else:                                   # «12000 люди» — сумма в начале
        h = _AMOUNT_HEAD.match(t)
        if not h:
            raise InputError("Не нашёл сумму. Пишите так: «Заборная сетка 75 м — 9900 нал».")
        amount_raw, what = h.group(1), h.group(2)
    amount = float(amount_raw.replace(" ", "").replace(",", "."))
    what = what.strip(" .,-—=:")
    if not what:
        raise InputError("Напишите, за что: «Чистка колодца 4600».")
    if amount <= 0 or amount > 10_000_000:
        raise InputError("Проверьте сумму.")
    return Amount(what, amount, method, unpaid)


def parse_money(text: str) -> float:
    """Только сумма: «50000», «50 000», «50000₽», «50к»."""
    t = (text or "").strip().lower().replace(" ", "").replace("₽", "").replace("руб", "").rstrip(".р")
    mult = 1000 if t.endswith(("к", "k", "т")) else 1
    t = t.rstrip("кkт")
    try:
        v = float(t.replace(",", ".")) * mult
    except ValueError:
        raise InputError("Напишите сумму числом: 50000")
    if v <= 0 or v > 10_000_000:
        raise InputError("Проверьте сумму.")
    return v


_QTY = re.compile(r"^\s*(\d+(?:[.,]\d+)?)\s*(т|тонн\w*|м3|м³|куб\w*|шт\w*|м|метр\w*|кг|л|меш\w*|рул\w*|пал\w*)\.?\s*$",
                  re.IGNORECASE)
# Длинные префиксы раньше коротких: «меш» и «метр» до «м».
_UNIT_NORM = [("т", "т"), ("м3", "м³"), ("м³", "м³"), ("куб", "м³"), ("шт", "шт"), ("меш", "мешок"),
              ("метр", "м"), ("м", "м"), ("кг", "кг"), ("л", "л"), ("рул", "рулон"), ("пал", "поддон")]


def parse_quantity(text: str) -> tuple[float, str]:
    """«82,6 т» → (82.6, 'т'); «40 мешков» → (40.0, 'мешок')."""
    m = _QTY.match(text or "")
    if not m:
        raise InputError("Напишите количество и единицу: «82,6 т», «12 м³», «40 мешков».")
    raw = m.group(2).lower()
    if raw.startswith("тонн"):
        raw = "т"
    unit = next(norm for pre, norm in _UNIT_NORM if raw.startswith(pre))
    return float(m.group(1).replace(",", ".")), unit


def cash_balance(user_id: int, issued: list[dict], expenses: list[dict]) -> float:
    """Остаток подотчёта: выдано человеку − потрачено им наличными и оплачено."""
    got = sum(_f(r.get("Сумма")) for r in issued if str(r.get("Кому ID")) == str(user_id))
    spent = sum(_f(r.get("Сумма")) for r in expenses
                if str(r.get("Автор ID")) == str(user_id)
                and str(r.get("Способ оплаты", "")).lower() == "наличные"
                and str(r.get("Оплачено", "")).lower() == "да")
    return round(got - spent, 2)


def _f(v) -> float:
    try:
        return float(str(v).replace(" ", "").replace(",", ".") or 0)
    except ValueError:
        return 0.0


def accumulated(rows: list[dict], object_id: str, what: str, unit: str, request_id: str = "") -> float:
    """Накопительно по материалу на объекте: по заявке, если есть, иначе по названию."""
    key = what.strip().lower()
    total = 0.0
    for r in rows:
        if str(r.get("Объект ID")) != object_id or str(r.get("Ед.", "")) != unit:
            continue
        same = (str(r.get("Заявка ID")) == request_id) if request_id else \
            str(r.get("Что принято", "")).strip().lower() == key
        if same:
            total += _f(r.get("Количество"))
    return round(total, 3)


def money(v: float) -> str:
    return f"{v:,.0f}".replace(",", " ") + " ₽"


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

"""Сценарии бота.

Четыре сценария по объекту — отчёт за день, заявка, приёмка, запись — и
сообщение об ошибке. Общая часть: выбор объекта, ввод текстом или голосом,
сбор фото, подтверждение перед записью в таблицу.

Защита данных:
- у каждого сценария своя метка (nonce) в callback-кнопках: кнопка из прошлого
  сценария не сработает в текущем;
- ID строки создаётся один раз на шаге подтверждения: повторное «Сохранить»
  после сбоя связи не пишет дубль (перед повтором проверяем, есть ли строка);
- события одного пользователя обрабатываются по очереди (SimpleEventIsolation
  в __main__.py), поэтому двойное нажатие не успевает записать две строки.

Зависимости приходят из Dispatcher: db (Storage), cfg (Config), user (User —
кладёт AccessMiddleware).
"""

from __future__ import annotations

import logging
import secrets
import time
from datetime import date, datetime, timedelta
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

from aiogram import BaseMiddleware, Bot, F, Router
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message, TelegramObject

from . import keyboards as kb
from .config import Config
from .domain import (ALL_OBJECTS_ROLES, APPROVE_ROLES, CASH_ISSUER_ROLES,
                     CASH_RECEIVER_ROLES, DELIVERY_FULL, DELIVERY_PART, EXPENSE_ARTICLES,
                     INVITE_ROLES, INVOICE_APPROVED, INVOICE_NEW, INVOICE_PAID,
                     INVOICE_REJECTED, INVOICE_ROLES, NOTE_TYPES, NOTIFY_INVOICE_ROLES,
                     NOTIFY_NOTE_ROLES, NOTIFY_NOTE_TYPES, NOTIFY_REQUEST_ROLES, PAY_METHODS,
                     PAY_ROLES, REQUEST_STATUS_NEW, REQUEST_TYPES, ROLES, SENDER_ROLES,
                     InputError, User, accumulated, archive_link, cash_balance, due_date_ok,
                     money, new_id, objects_for, parse_amount, parse_due_date,
                     parse_machinery, parse_money, parse_people, parse_quantity,
                     users_to_notify)
from .storage import Storage, StorageUnavailable

log = logging.getLogger(__name__)

service = Router(name="service")   # /chatid — работает в группе и канале
menu = Router(name="menu")         # проверяется раньше сценариев: кнопка меню
router = Router(name="flows")      # посреди сценария начинает новый
# Сценарии — только в личке. В группе или канале-архиве бот лишь хранит копии фото.
menu.message.filter(F.chat.type == "private")
router.message.filter(F.chat.type == "private")
routers = [service, menu, router]

MAX_FILES = 10
MAX_SUMMARY = 3500          # лимит сообщения Telegram — 4096 знаков
UNAVAILABLE = "Таблица сейчас недоступна. Попробуйте через пару минут."


# ── Состояния ─────────────────────────────────────────────────────────────────

class Flow(StatesGroup):
    obj = State()          # выбор объекта — общий для всех сценариев


class Report(StatesGroup):
    people = State()
    crew = State()
    machinery = State()
    done = State()
    problems = State()
    photos = State()
    confirm = State()


class Req(StatesGroup):
    rtype = State()
    what = State()
    due = State()
    urgent = State()
    stage = State()
    files = State()
    confirm = State()


class Accept(StatesGroup):
    request = State()
    what = State()
    full = State()
    qty = State()
    comment = State()
    photos = State()
    confirm = State()


class Note(StatesGroup):
    ntype = State()
    text = State()
    photos = State()
    confirm = State()


class Bug(StatesGroup):
    text = State()


class Expense(StatesGroup):
    article = State()
    what = State()
    method = State()
    paid = State()
    photos = State()
    confirm = State()


class Cash(StatesGroup):
    who = State()
    amount = State()
    obj = State()
    purpose = State()
    confirm = State()


class Invoice(StatesGroup):
    file = State()
    what = State()
    confirm = State()


class Invite(StatesGroup):
    role = State()
    objects = State()


# ── Доступ ────────────────────────────────────────────────────────────────────

class AccessMiddleware(BaseMiddleware):
    """Пускает дальше только активных пользователей из листа «Пользователи».

    Незнакомый пользователь попадает в лист с «Активен = нет»; администраторам
    уходит уведомление. Помощник вписывает роль и объекты и ставит «да».
    """

    async def __call__(self, handler: Callable[[TelegramObject, dict], Awaitable[Any]],
                       event: TelegramObject, data: dict) -> Any:
        tg_user = getattr(event, "from_user", None)
        if tg_user is None:
            return None
        if isinstance(event, Message):
            if event.text and event.text.split("@")[0] in {"/myid", "/chatid"}:
                return await handler(event, data)
            if event.chat.type != "private":
                return None          # группы: никого не регистрируем, не отвечаем
        msg = event if isinstance(event, Message) else event.message
        if isinstance(event, CallbackQuery):
            await event.answer()
        db: Storage = data["db"]
        cfg: Config = data["cfg"]
        try:
            users = await db.list_users()
            user = next((u for u in users if u.telegram_id == tg_user.id), None)
            if user and user.active and user.role in ROLES:
                data["user"] = user
                kb.CURRENT_ROLE.set(user.role)
                return await handler(event, data)
            invite = _invite_code(event)
            if invite:
                text = await _accept_invite(invite, tg_user, user, db, cfg, data["bot"])
                if msg is not None:
                    await msg.answer(text, reply_markup=kb.main_menu(kb.CURRENT_ROLE.get()))
                return None
            if user is None:
                name = " ".join(filter(None, [tg_user.first_name, tg_user.last_name])) or str(tg_user.id)
                await db.add_pending_user(tg_user.id, name, tg_user.username or "",
                                          _now(cfg).strftime("%Y-%m-%d %H:%M"))
                await _notify_admins(data["bot"], cfg, users,
                                     f"🆕 Новый пользователь: {name} (@{tg_user.username or '—'}), "
                                     f"ID {tg_user.id}.\nВпишите роль и объекты в лист «Пользователи» "
                                     f"и поставьте «Активен = да».")
                text = "Здравствуйте! Заявка на доступ отправлена. Как только вас добавят, нажмите /start."
            elif user.active:
                text = (f"Роль «{user.role}» не распознана. Помощник должен вписать одну из: "
                        f"{', '.join(sorted(ROLES))}.")
            else:
                text = "Доступ ещё не открыт. Когда помощник добавит вас, нажмите /start."
        except StorageUnavailable:
            text = UNAVAILABLE
        if msg is not None:
            await msg.answer(text)
        return None


# ── Общие помощники ───────────────────────────────────────────────────────────

def _now(cfg: Config) -> datetime:
    return datetime.now(ZoneInfo(cfg.tz))


def _clip(text: str, limit: int = MAX_SUMMARY) -> str:
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _cb(kind: str, nonce: str, payload: str = "") -> str:
    return f"{kind}:{nonce}:{payload}"


async def _arg(call: CallbackQuery, state: FSMContext) -> str | None:
    """Payload кнопки, если она из текущего сценария; иначе None («устарела»)."""
    parts = (call.data or "").split(":", 2)
    nonce = (await state.get_data()).get("nonce")
    if len(parts) != 3 or not nonce or parts[1] != nonce:
        await call.message.answer("Эта кнопка из прошлого сообщения. Ответьте на последний вопрос "
                                  f"или нажмите «{kb.BTN_CANCEL}».")
        return None
    return parts[2]


def _pick(items: list, payload: str):
    try:
        return items[int(payload)]
    except (ValueError, IndexError, TypeError):
        return None


async def _close_buttons(call: CallbackQuery, text: str | None = None) -> None:
    """Убрать кнопки у сообщения. Сбой здесь не должен ломать сценарий."""
    try:
        if text is not None:
            await call.message.edit_text(text)
        else:
            await call.message.edit_reply_markup()
    except Exception as e:
        log.debug("не отредактировано: %s", e)


async def _send(bot: Bot, chat_id: int, text: str) -> None:
    try:
        await bot.send_message(chat_id, _clip(text))
    except Exception as e:  # пользователь не начал диалог с ботом и т.п.
        log.warning("не отправлено %s: %s", chat_id, e)


async def _notify_admins(bot: Bot, cfg: Config, users: list[User], text: str) -> None:
    ids = set(cfg.admin_ids) | {u.telegram_id for u in users
                                if u.active and u.role in {"директор", "помощник"}}
    for tid in ids:
        await _send(bot, tid, text)


async def _notify(bot: Bot, users: list[User], text: str) -> None:
    for u in users:
        await _send(bot, u.telegram_id, text)


_archive_alert_at = 0.0


async def _archive(message: Message, bot: Bot, cfg: Config, caption: str) -> dict:
    """Копирует фото/файл/голос в чат-архив. Возвращает {'link', 'file_id'}."""
    global _archive_alert_at
    media = (message.photo[-1] if message.photo else None) or message.document or \
        message.voice or message.audio or message.video or message.video_note
    file_id = media.file_id if media else ""
    link = ""
    if cfg.archive_chat_id:
        try:
            copied = await bot.copy_message(cfg.archive_chat_id, message.chat.id,
                                            message.message_id, caption=caption[:1000])
            link = archive_link(cfg.archive_chat_id, copied.message_id)
        except Exception as e:
            log.warning("архив недоступен: %s", e)
            if not _archive_alert_at or time.monotonic() - _archive_alert_at > 3600:
                _archive_alert_at = time.monotonic()
                for tid in cfg.admin_ids:
                    await _send(bot, tid, f"⚠️ Архив фото недоступен: {e}. Фото пишутся в таблицу "
                                          "только как file_id. Проверьте, что бот — админ архива "
                                          "и ARCHIVE_CHAT_ID верный (/chatid в архиве).")
    return {"link": link, "file_id": file_id}


async def _read_text(message: Message, bot: Bot, cfg: Config, caption: str) -> str | None:
    """Текст сообщения; голосовое — сохраняем в архив и пишем ссылку."""
    if message.text:
        return message.text.strip()
    if message.voice or message.audio or message.video_note:
        media = await _archive(message, bot, cfg, caption)
        return f"🎤 голосовое: {media['link'] or media['file_id']}"
    return None


def _links(files: list[dict]) -> str:
    return "\n".join(f["link"] for f in files if f["link"])


def _file_ids(files: list[dict]) -> str:
    return "\n".join(f["file_id"] for f in files if f["file_id"])


async def _to_menu(message: Message, state: FSMContext, text: str) -> None:
    await state.clear()
    await message.answer(text, reply_markup=kb.main_menu())


async def _ask_confirm(message: Message, state: FSMContext, cfg: Config, prefix: str,
                       summary: str) -> None:
    """Шаг подтверждения: здесь же создаётся ID будущей строки."""
    d = await state.get_data()
    if not d.get("row_id"):
        await state.update_data(row_id=new_id(prefix, _now(cfg)), attempts=0)
    nonce = d["nonce"]
    await message.answer("Проверьте:", reply_markup=kb.step())
    await message.answer(_clip(summary), reply_markup=kb.inline(
        [("✅ Сохранить", _cb("save", nonce)), ("❌ Отменить", _cb("drop", nonce))], per_row=2))


async def _save(call: CallbackQuery, state: FSMContext, db: Storage, sheet: str,
                row: dict) -> bool:
    """Запись строки. True — строка в таблице (записана сейчас или раньше).

    При ошибке состояние остаётся, «Сохранить» можно нажать ещё раз; перед
    повтором проверяем, не дошла ли первая попытка.
    """
    d = await state.get_data()
    try:
        if d.get("attempts", 0) and await db.exists(sheet, row["ID"]):
            return True
        await state.update_data(attempts=d.get("attempts", 0) + 1)
        await db.append(sheet, row)
        return True
    except Exception:
        log.exception("ошибка записи в %s", sheet)
        await call.message.answer("Не получилось сохранить — нет связи с таблицей. "
                                  "Нажмите «Сохранить» ещё раз через минуту.")
        return False


async def _finish(call: CallbackQuery, state: FSMContext, text: str) -> None:
    await state.clear()
    await _close_buttons(call)
    await call.message.answer(text, reply_markup=kb.main_menu())


def _can_send(user: User) -> bool:
    return user.role in SENDER_ROLES


# ── Служебные команды ─────────────────────────────────────────────────────────

@service.message(Command("chatid"))
@service.channel_post(Command("chatid"))
async def cmd_chatid(message: Message) -> None:
    """ID чата — для ARCHIVE_CHAT_ID: бот — админ архива, в архиве написать /chatid."""
    await message.answer(f"ID этого чата: {message.chat.id}")


HELP = (
    "Что умеет бот:\n"
    f"{kb.BTN_REPORT} — люди, техника и часы, что сделано, проблемы, фото.\n"
    f"{kb.BTN_REQUEST} — что купить или заказать (материал, подрядчик, техника).\n"
    f"{kb.BTN_ACCEPT} — что привезли на объект, фото накладной.\n"
    f"{kb.BTN_NOTE} — проблема, допработа, ждём от заказчика, замечание.\n"
    f"{kb.BTN_BUG} — что-то в боте не так.\n\n"
    "Где можно — отвечайте голосом. /cancel — отменить, /myid — ваш Telegram ID."
)


@menu.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, user: User) -> None:
    extra = "" if _can_send(user) else ("\n\nВаша роль получает уведомления; отправлять данные "
                                        "по объектам могут прорабы, РП, директор и помощник.")
    await _to_menu(message, state, f"{user.name}, вы в системе как «{user.role}».\n\n{HELP}{extra}")


@menu.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(HELP, reply_markup=kb.main_menu())


@menu.message(Command("myid"))
async def cmd_myid(message: Message) -> None:
    await message.answer(f"Ваш Telegram ID: {message.from_user.id}")


@menu.message(Command("cancel"))
@menu.message(F.text == kb.BTN_CANCEL)
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await _to_menu(message, state, "Отменено.")


@menu.callback_query(F.data.startswith("drop:"))
async def cb_drop(call: CallbackQuery, state: FSMContext) -> None:
    if await _arg(call, state) is None:
        return
    await _close_buttons(call)
    await _to_menu(call.message, state, "Отменено, ничего не сохранено.")


# ── Выбор объекта ─────────────────────────────────────────────────────────────

async def _start_flow(message: Message, state: FSMContext, db: Storage, user: User,
                      flow: str, roles: set[str] = SENDER_ROLES, obj_id: str | None = None) -> None:
    await state.clear()
    if user.role not in roles:
        await message.answer("Отправлять данные по объектам могут прорабы, РП, директор и помощник. "
                             f"Если это ошибка — напишите помощнику или нажмите «{kb.BTN_BUG}».",
                             reply_markup=kb.main_menu())
        return
    try:
        objs = objects_for(user, await db.list_objects())
    except StorageUnavailable:
        await message.answer(UNAVAILABLE, reply_markup=kb.main_menu())
        return
    if not objs:
        await message.answer("За вами пока не закреплён ни один объект. "
                             "Напишите помощнику.", reply_markup=kb.main_menu())
        return
    nonce = secrets.token_hex(3)
    await state.update_data(flow=flow, nonce=nonce, objects=[(o.id, o.name) for o in objs], files=[])
    preset = next((o for o in objs if o.id == obj_id), None)
    if preset or len(objs) == 1:
        o = preset or objs[0]
        await state.update_data(obj_id=o.id, obj_name=o.name)
        await message.answer(f"Объект: {o.name}")
        await _after_object(message, state, db)
        return
    await state.set_state(Flow.obj)
    await message.answer("Выберите объект:", reply_markup=kb.step())
    await message.answer("Объекты:", reply_markup=kb.inline(
        [(o.name, _cb("obj", nonce, str(i))) for i, o in enumerate(objs)]))


@router.callback_query(Flow.obj, F.data.startswith("obj:"))
async def cb_object(call: CallbackQuery, state: FSMContext, db: Storage) -> None:
    payload = await _arg(call, state)
    if payload is None:
        return
    picked = _pick((await state.get_data())["objects"], payload)
    if picked is None:
        await call.message.answer("Кнопка устарела, начните заново.")
        return
    oid, name = picked
    await state.update_data(obj_id=oid, obj_name=name)
    await _close_buttons(call, f"Объект: {name}")
    await _after_object(call.message, state, db)


async def _after_object(message: Message, state: FSMContext, db: Storage) -> None:
    d = await state.get_data()
    flow, nonce = d["flow"], d["nonce"]
    if flow == "report":
        await state.set_state(Report.people)
        await message.answer("Сколько людей работало сегодня? Числом.",
                             reply_markup=kb.step("0"))
    elif flow == "request":
        await state.set_state(Req.rtype)
        await message.answer("Что нужно?", reply_markup=kb.step())
        await message.answer("Тип заявки:", reply_markup=kb.inline(
            [(t.capitalize(), _cb("rt", nonce, str(i))) for i, t in enumerate(REQUEST_TYPES)], per_row=2))
    elif flow == "accept":
        await _ask_accept_request(message, state, db)
    elif flow == "expense":
        await state.set_state(Expense.article)
        await message.answer("На что расход?", reply_markup=kb.step())
        await message.answer("Статья:", reply_markup=kb.inline(
            [(t.capitalize(), _cb("ea", nonce, str(i))) for i, t in enumerate(EXPENSE_ARTICLES)], per_row=3))
    elif flow == "invoice":
        await state.set_state(Invoice.file)
        await message.answer("Пришлите счёт: PDF, фото или скриншот.", reply_markup=kb.step())
    elif flow == "note":
        await state.set_state(Note.ntype)
        await message.answer("О чём запись?", reply_markup=kb.step())
        await message.answer("Тип:", reply_markup=kb.inline(
            [(t.capitalize(), _cb("nt", nonce, str(i))) for i, t in enumerate(NOTE_TYPES)], per_row=2))


# ── Сбор фото и файлов (общий для сценариев) ──────────────────────────────────

MEDIA = F.photo | F.document | F.video | F.video_note


async def _collect_media(message: Message, state: FSMContext, bot: Bot, cfg: Config,
                         user: User) -> None:
    data = await state.get_data()
    files = data.get("files", [])
    if len(files) >= MAX_FILES:
        await message.answer(f"Не больше {MAX_FILES} файлов, лишние не сохранены. "
                             f"Нажмите «{kb.BTN_DONE}».")
        return
    caption = f"{data.get('obj_name', '')} · {user.name} · {data.get('flow', '')} · {_now(cfg):%Y-%m-%d %H:%M}"
    files.append(await _archive(message, bot, cfg, caption))
    await state.update_data(files=files)
    await message.answer(f"Принято: {len(files)}. Ещё — или «{kb.BTN_DONE}».",
                         reply_markup=kb.step(kb.BTN_DONE))


# ── Отчёт за день ─────────────────────────────────────────────────────────────

@menu.message(F.text == kb.BTN_REPORT)
async def report_start(message: Message, state: FSMContext, db: Storage, user: User) -> None:
    await _start_flow(message, state, db, user, "report")


@router.message(Report.people)
async def report_people(message: Message, state: FSMContext) -> None:
    try:
        n = parse_people(message.text or "")
    except InputError as e:
        await message.answer(str(e))
        return
    await state.update_data(people=n)
    await state.set_state(Report.crew)
    await message.answer("Какая бригада или подрядчик?", reply_markup=kb.step(kb.BTN_SKIP))


@router.message(Report.crew)
async def report_crew(message: Message, state: FSMContext, bot: Bot, cfg: Config) -> None:
    text = await _read_text(message, bot, cfg, "отчёт: бригада")
    if not text:
        await message.answer("Напишите текстом или голосовым.")
        return
    await state.update_data(crew="" if text == kb.BTN_SKIP else text)
    await state.set_state(Report.machinery)
    await message.answer("Техника: часы или рейсы и цена, каждая машина с новой строки или через "
                         "запятую. Пример:\n«Экскаватор 9 ч по 3500\nКаток 10 ч по 3500\n"
                         "Хово 5 рейсов по 10000».\nЦену можно не писать. Если техники не было — «Нет».",
                         reply_markup=kb.step(kb.BTN_NO))


@router.message(Report.machinery)
async def report_machinery(message: Message, state: FSMContext) -> None:
    if not message.text:
        await message.answer("Технику — текстом, чтобы посчитать часы и деньги. "
                             "Пример: «Экскаватор 9 ч по 3500».")
        return
    try:
        rep = parse_machinery(message.text)
    except InputError as e:
        await message.answer(str(e))
        return
    await state.update_data(machinery=str(rep), machine_hours=rep.hours,
                            trips=rep.trips, machine_cost=rep.cost)
    await state.set_state(Report.done)
    await message.answer("Что сделано за день? Можно голосом.", reply_markup=kb.step())


@router.message(Report.done)
async def report_done(message: Message, state: FSMContext, bot: Bot, cfg: Config) -> None:
    text = await _read_text(message, bot, cfg, "отчёт: сделано")
    if not text:
        await message.answer("Напишите текстом или голосовым.")
        return
    await state.update_data(done=text)
    await state.set_state(Report.problems)
    await message.answer("Проблемы, простои, допработы? Если нет — «Нет».",
                         reply_markup=kb.step(kb.BTN_NO))


@router.message(Report.problems)
async def report_problems(message: Message, state: FSMContext, bot: Bot, cfg: Config) -> None:
    text = await _read_text(message, bot, cfg, "отчёт: проблемы")
    if not text:
        await message.answer("Напишите текстом или голосовым.")
        return
    await state.update_data(problems="" if text == kb.BTN_NO else text)
    await state.set_state(Report.photos)
    await message.answer("Пришлите фото работ (можно несколько), затем «Готово».",
                         reply_markup=kb.step(kb.BTN_NO_PHOTO))


@router.message(Report.photos, MEDIA)
async def report_photo(message: Message, state: FSMContext, bot: Bot, cfg: Config, user: User) -> None:
    await _collect_media(message, state, bot, cfg, user)


@router.message(Report.photos, F.text.in_({kb.BTN_DONE, kb.BTN_NO_PHOTO}))
async def report_photos_done(message: Message, state: FSMContext, cfg: Config) -> None:
    d = await state.get_data()
    await state.set_state(Report.confirm)
    await _ask_confirm(message, state, cfg, "ОТ",
                       f"📋 Отчёт · {d['obj_name']}\n"
                       f"Людей: {d['people']}\n"
                       f"Бригада: {d['crew'] or '—'}\n"
                       f"Техника: {d['machinery'] or 'нет'}\n"
                       f"  итого {d['machine_hours']:g} маш.-ч, {d['trips']:g} рейсов, {d['machine_cost']:g} ₽\n"
                       f"Сделано: {d['done']}\n"
                       f"Проблемы: {d['problems'] or 'нет'}\n"
                       f"Фото: {len(d['files'])}")


@router.callback_query(Report.confirm, F.data.startswith("save:"))
async def report_save(call: CallbackQuery, state: FSMContext, db: Storage, cfg: Config,
                      user: User, bot: Bot) -> None:
    if await _arg(call, state) is None:
        return
    d = await state.get_data()
    now = _now(cfg)
    row = {"ID": d["row_id"], "Дата": now.strftime("%Y-%m-%d"),
           "Время": now.strftime("%H:%M"), "Объект ID": d["obj_id"], "Объект": d["obj_name"],
           "Автор ID": user.telegram_id, "Автор": user.name, "Роль": user.role,
           "Людей": d["people"], "Бригада / подрядчик": d["crew"],
           "Техника": d["machinery"], "Машино-часы": d["machine_hours"],
           "Рейсы": d["trips"], "Техника, ₽": d["machine_cost"],
           "Сделано": d["done"], "Проблемы": d["problems"], "Фото, шт": len(d["files"]),
           "Фото (ссылки)": _links(d["files"]), "Файлы (file_id)": _file_ids(d["files"])}
    if not await _save(call, state, db, "Отчёты", row):
        return
    await _finish(call, state, f"Отчёт сохранён ({row['ID']}). Спасибо!")
    if d["problems"]:
        users = await _users_or_empty(db)
        await _notify(bot, users_to_notify(users, NOTIFY_NOTE_ROLES, d["obj_id"], user.telegram_id),
                      f"⚠️ {d['obj_name']} · {user.name} в отчёте за день:\n{d['problems']}")


async def _users_or_empty(db: Storage) -> list[User]:
    try:
        return await db.list_users()
    except StorageUnavailable:
        return []


# ── Заявка ────────────────────────────────────────────────────────────────────

@menu.message(F.text == kb.BTN_REQUEST)
async def req_start(message: Message, state: FSMContext, db: Storage, user: User) -> None:
    await _start_flow(message, state, db, user, "request")


@router.callback_query(Req.rtype, F.data.startswith("rt:"))
async def req_type(call: CallbackQuery, state: FSMContext) -> None:
    payload = await _arg(call, state)
    rtype = _pick(REQUEST_TYPES, payload) if payload is not None else None
    if rtype is None:
        return
    await _close_buttons(call, f"Тип: {rtype}")
    await state.update_data(rtype=rtype)
    await state.set_state(Req.what)
    await call.message.answer("Что нужно и сколько? Можно голосом.\n"
                              "Пример: «цемент М500 — 40 мешков».", reply_markup=kb.step())


@router.message(Req.what)
async def req_what(message: Message, state: FSMContext, bot: Bot, cfg: Config) -> None:
    text = await _read_text(message, bot, cfg, "заявка: что нужно")
    if not text:
        await message.answer("Напишите текстом или голосовым.")
        return
    await state.update_data(what=text)
    await state.set_state(Req.due)
    today = _now(cfg).date()
    nonce = (await state.get_data())["nonce"]
    opts = [(label, _cb("due", nonce, (today + timedelta(days=n)).isoformat()))
            for label, n in [("Завтра", 1), ("Через 3 дня", 3), ("Через неделю", 7),
                             ("Через 2 недели", 14)]]
    await message.answer("К какой дате нужно? Кнопкой или напишите дату (15.10).",
                         reply_markup=kb.step())
    await message.answer("Срок:", reply_markup=kb.inline(opts, per_row=2))


async def _ask_urgent(message: Message, state: FSMContext, due: date) -> None:
    await state.update_data(due=due.isoformat())
    await state.set_state(Req.urgent)
    nonce = (await state.get_data())["nonce"]
    await message.answer("Срочно?", reply_markup=kb.inline(
        [("Да, срочно", _cb("urg", nonce, "1")), ("Нет", _cb("urg", nonce, "0"))], per_row=2))


@router.callback_query(Req.due, F.data.startswith("due:"))
async def req_due_button(call: CallbackQuery, state: FSMContext, cfg: Config) -> None:
    payload = await _arg(call, state)
    if payload is None:
        return
    try:
        due = date.fromisoformat(payload)
    except ValueError:
        return
    if not due_date_ok(due, _now(cfg).date()):
        await call.message.answer("Эта кнопка устарела. Выберите срок ещё раз или напишите дату.")
        return
    await _close_buttons(call, f"Нужно к: {due:%d.%m.%Y}")
    await _ask_urgent(call.message, state, due)


@router.message(Req.due, F.text)
async def req_due_text(message: Message, state: FSMContext, cfg: Config) -> None:
    try:
        due = parse_due_date(message.text, _now(cfg).date())
    except InputError as e:
        await message.answer(str(e))
        return
    await _ask_urgent(message, state, due)


@router.callback_query(Req.urgent, F.data.startswith("urg:"))
async def req_urgent(call: CallbackQuery, state: FSMContext) -> None:
    payload = await _arg(call, state)
    if payload not in {"0", "1"}:
        return
    urgent = payload == "1"
    await _close_buttons(call, "Срочно: да" if urgent else "Срочно: нет")
    await state.update_data(urgent=urgent)
    await state.set_state(Req.stage)
    await call.message.answer("Для какого этапа работ? Например «Опалубка опор».",
                              reply_markup=kb.step(kb.BTN_SKIP))


@router.message(Req.stage)
async def req_stage(message: Message, state: FSMContext, bot: Bot, cfg: Config) -> None:
    text = await _read_text(message, bot, cfg, "заявка: этап")
    if not text:
        await message.answer("Напишите текстом или голосовым.")
        return
    await state.update_data(stage="" if text == kb.BTN_SKIP else text)
    await state.set_state(Req.files)
    await message.answer("Фото или файл (спецификация, чертёж, фото образца)? "
                         "Пришлите или нажмите «Без фото».", reply_markup=kb.step(kb.BTN_NO_PHOTO))


@router.message(Req.files, MEDIA)
async def req_file(message: Message, state: FSMContext, bot: Bot, cfg: Config, user: User) -> None:
    await _collect_media(message, state, bot, cfg, user)


def _req_summary(d: dict) -> str:
    return (f"🧱 Заявка · {d['obj_name']}{' · 🔥 СРОЧНО' if d['urgent'] else ''}\n"
            f"Тип: {d['rtype']}\n"
            f"Что: {d['what']}\n"
            f"Нужно к: {date.fromisoformat(d['due']):%d.%m.%Y}\n"
            f"Этап: {d['stage'] or '—'}\n"
            f"Файлов: {len(d['files'])}")


@router.message(Req.files, F.text.in_({kb.BTN_DONE, kb.BTN_NO_PHOTO}))
async def req_files_done(message: Message, state: FSMContext, cfg: Config) -> None:
    d = await state.get_data()
    await state.set_state(Req.confirm)
    await _ask_confirm(message, state, cfg, "ЗК", _req_summary(d))


@router.callback_query(Req.confirm, F.data.startswith("save:"))
async def req_save(call: CallbackQuery, state: FSMContext, db: Storage, cfg: Config,
                   user: User, bot: Bot) -> None:
    if await _arg(call, state) is None:
        return
    d = await state.get_data()
    row = {"ID": d["row_id"], "Создана": _now(cfg).strftime("%Y-%m-%d %H:%M"),
           "Объект ID": d["obj_id"], "Объект": d["obj_name"], "Автор ID": user.telegram_id,
           "Автор": user.name, "Роль": user.role, "Тип": d["rtype"], "Что нужно": d["what"],
           "Нужно к": d["due"], "Срочно": "да" if d["urgent"] else "нет", "Этап": d["stage"],
           "Файлы (ссылки)": _links(d["files"]), "Файлы (file_id)": _file_ids(d["files"]),
           "Статус": REQUEST_STATUS_NEW, "Поставка": "нет"}
    if not await _save(call, state, db, "Заявки", row):
        return
    await _finish(call, state, f"Заявка сохранена ({row['ID']}), снабжение получило уведомление.")
    users = await _users_or_empty(db)
    await _notify(bot, users_to_notify(users, NOTIFY_REQUEST_ROLES, d["obj_id"], user.telegram_id),
                  f"Новая заявка {row['ID']} от {user.name}\n{_req_summary(d)}\n"
                  f"{_links(d['files'])}".strip())


# ── Приёмка ───────────────────────────────────────────────────────────────────

@menu.message(F.text == kb.BTN_ACCEPT)
async def accept_start(message: Message, state: FSMContext, db: Storage, user: User) -> None:
    await _start_flow(message, state, db, user, "accept")


async def _ask_accept_request(message: Message, state: FSMContext, db: Storage) -> None:
    d = await state.get_data()
    try:
        open_reqs = (await db.open_requests(d["obj_id"]))[-10:]
    except StorageUnavailable:
        open_reqs = []
        await message.answer("Список заявок сейчас недоступен — можно оформить приёмку без заявки.")
    await state.update_data(reqs=[(r["ID"], str(r.get("Что нужно", ""))[:1000]) for r in open_reqs])
    await state.set_state(Accept.request)
    nonce = d["nonce"]
    opts = [(f"{r['ID']}: {str(r.get('Что нужно', ''))[:40]}", _cb("ar", nonce, str(i)))
            for i, r in enumerate(open_reqs)]
    opts.append(("Без заявки", _cb("ar", nonce, "-")))
    await message.answer("По какой заявке привезли?", reply_markup=kb.step())
    await message.answer("Открытые заявки по объекту:" if open_reqs else "Открытых заявок нет.",
                         reply_markup=kb.inline(opts))


@router.callback_query(Accept.request, F.data.startswith("ar:"))
async def accept_request(call: CallbackQuery, state: FSMContext) -> None:
    payload = await _arg(call, state)
    if payload is None:
        return
    if payload == "-":
        await state.update_data(req_id="")
        await _close_buttons(call, "Без заявки")
        await state.set_state(Accept.what)
        await call.message.answer("Что привезли и сколько?", reply_markup=kb.step())
        return
    picked = _pick((await state.get_data())["reqs"], payload)
    if picked is None:
        await call.message.answer("Кнопка устарела, выберите заявку ещё раз.")
        return
    rid, what = picked
    await state.update_data(req_id=rid, what=what)
    await _close_buttons(call, _clip(f"Заявка {rid}: {what}"))
    await _ask_full(call.message, state)


@router.message(Accept.what)
async def accept_what(message: Message, state: FSMContext, bot: Bot, cfg: Config) -> None:
    text = await _read_text(message, bot, cfg, "приёмка: что привезли")
    if not text:
        await message.answer("Напишите текстом или голосовым.")
        return
    await state.update_data(what=text)
    await _ask_full(message, state)


async def _ask_full(message: Message, state: FSMContext) -> None:
    await state.set_state(Accept.full)
    nonce = (await state.get_data())["nonce"]
    await message.answer("Привезли полностью?", reply_markup=kb.inline(
        [("Полностью", _cb("full", nonce, "1")), ("Частично", _cb("full", nonce, "0"))], per_row=2))


@router.callback_query(Accept.full, F.data.startswith("full:"))
async def accept_full(call: CallbackQuery, state: FSMContext) -> None:
    payload = await _arg(call, state)
    if payload not in {"0", "1"}:
        return
    full = payload == "1"
    await _close_buttons(call, "Полностью" if full else "Частично")
    await state.update_data(full=full)
    await state.set_state(Accept.qty)
    await call.message.answer("Сколько привезли? «82,6 т», «12 м³», «40 мешков». "
                              "Бот посчитает накопительный итог.", reply_markup=kb.step(kb.BTN_SKIP))


@router.message(Accept.qty, F.text)
async def accept_qty(message: Message, state: FSMContext) -> None:
    if message.text == kb.BTN_SKIP:
        await state.update_data(qty="", unit="")
    else:
        try:
            qty, unit = parse_quantity(message.text)
        except InputError as e:
            await message.answer(str(e))
            return
        await state.update_data(qty=qty, unit=unit)
    await state.set_state(Accept.comment)
    await message.answer("Комментарий: брак, недовоз, пересорт? Если нет — «Нет».",
                         reply_markup=kb.step(kb.BTN_NO))


@router.message(Accept.comment)
async def accept_comment(message: Message, state: FSMContext, bot: Bot, cfg: Config) -> None:
    text = await _read_text(message, bot, cfg, "приёмка: комментарий")
    if not text:
        await message.answer("Напишите текстом или голосовым.")
        return
    await state.update_data(comment="" if text == kb.BTN_NO else text)
    await state.set_state(Accept.photos)
    await message.answer("Фото накладной (УПД, ТТН) и груза, затем «Готово».",
                         reply_markup=kb.step(kb.BTN_NO_PHOTO))


@router.message(Accept.photos, MEDIA)
async def accept_photo(message: Message, state: FSMContext, bot: Bot, cfg: Config, user: User) -> None:
    await _collect_media(message, state, bot, cfg, user)


@router.message(Accept.photos, F.text.in_({kb.BTN_DONE, kb.BTN_NO_PHOTO}))
async def accept_photos_done(message: Message, state: FSMContext, cfg: Config) -> None:
    d = await state.get_data()
    await state.set_state(Accept.confirm)
    qty_txt = f"{d['qty']:g} {d['unit']}" if d["qty"] != "" else "—"
    await _ask_confirm(message, state, cfg, "ПР",
                       f"✅ Приёмка · {d['obj_name']}\n"
                       f"Заявка: {d['req_id'] or 'без заявки'}\n"
                       f"Что: {d['what']}\n"
                       f"Количество: {qty_txt}\n"
                       f"{'Полностью' if d['full'] else 'Частично'}\n"
                       f"Комментарий: {d['comment'] or '—'}\n"
                       f"Фото: {len(d['files'])}")


@router.callback_query(Accept.confirm, F.data.startswith("save:"))
async def accept_save(call: CallbackQuery, state: FSMContext, db: Storage, cfg: Config,
                      user: User, bot: Bot) -> None:
    if await _arg(call, state) is None:
        return
    d = await state.get_data()
    delivery = DELIVERY_FULL if d["full"] else DELIVERY_PART
    row = {"ID": d["row_id"], "Дата": _now(cfg).strftime("%Y-%m-%d %H:%M"),
           "Объект ID": d["obj_id"], "Объект": d["obj_name"], "Автор ID": user.telegram_id,
           "Автор": user.name, "Заявка ID": d["req_id"], "Что принято": d["what"],
           "Количество": d["qty"], "Ед.": d["unit"], "Полностью / частично": delivery, "Комментарий": d["comment"],
           "Фото (ссылки)": _links(d["files"]), "Файлы (file_id)": _file_ids(d["files"])}
    if not await _save(call, state, db, "Приёмка", row):
        return
    total_line = ""
    if d["qty"] != "":
        try:
            total = accumulated(await db.rows("Приёмка"), d["obj_id"], d["what"], d["unit"], d["req_id"])
            total_line = f"\nНакопительно по «{d['what'][:40]}»: {total:g} {d['unit']}"
        except StorageUnavailable:
            pass
    await _finish(call, state, f"Приёмка сохранена ({row['ID']}).{total_line}")
    if d["req_id"]:
        try:
            await db.update("Заявки", "ID", d["req_id"], {"Поставка": delivery})
        except Exception:
            log.exception("не обновлена поставка в заявке %s", d["req_id"])
    users = await _users_or_empty(db)
    await _notify(bot, users_to_notify(users, NOTIFY_REQUEST_ROLES, d["obj_id"], user.telegram_id),
                  f"✅ {d['obj_name']}: {user.name} принял «{d['what']}» — {delivery}."
                  f"{' Заявка ' + d['req_id'] + '.' if d['req_id'] else ''}"
                  f"{chr(10) + 'Комментарий: ' + d['comment'] if d['comment'] else ''}")


# ── Запись по объекту ─────────────────────────────────────────────────────────

@menu.message(F.text == kb.BTN_NOTE)
async def note_start(message: Message, state: FSMContext, db: Storage, user: User) -> None:
    await _start_flow(message, state, db, user, "note")


@router.callback_query(Note.ntype, F.data.startswith("nt:"))
async def note_type(call: CallbackQuery, state: FSMContext) -> None:
    payload = await _arg(call, state)
    ntype = _pick(NOTE_TYPES, payload) if payload is not None else None
    if ntype is None:
        return
    await _close_buttons(call, f"Тип: {ntype}")
    await state.update_data(ntype=ntype)
    await state.set_state(Note.text)
    await call.message.answer("Опишите. Можно голосом.", reply_markup=kb.step())


@router.message(Note.text)
async def note_text(message: Message, state: FSMContext, bot: Bot, cfg: Config) -> None:
    text = await _read_text(message, bot, cfg, "запись")
    if not text:
        await message.answer("Напишите текстом или голосовым.")
        return
    await state.update_data(text=text)
    await state.set_state(Note.photos)
    await message.answer("Фото, затем «Готово».", reply_markup=kb.step(kb.BTN_NO_PHOTO))


@router.message(Note.photos, MEDIA)
async def note_photo(message: Message, state: FSMContext, bot: Bot, cfg: Config, user: User) -> None:
    await _collect_media(message, state, bot, cfg, user)


@router.message(Note.photos, F.text.in_({kb.BTN_DONE, kb.BTN_NO_PHOTO}))
async def note_photos_done(message: Message, state: FSMContext, cfg: Config) -> None:
    d = await state.get_data()
    await state.set_state(Note.confirm)
    await _ask_confirm(message, state, cfg, "ЗП",
                       f"📝 {d['ntype'].capitalize()} · {d['obj_name']}\n{d['text']}\n"
                       f"Фото: {len(d['files'])}")


@router.callback_query(Note.confirm, F.data.startswith("save:"))
async def note_save(call: CallbackQuery, state: FSMContext, db: Storage, cfg: Config,
                    user: User, bot: Bot) -> None:
    if await _arg(call, state) is None:
        return
    d = await state.get_data()
    row = {"ID": d["row_id"], "Дата": _now(cfg).strftime("%Y-%m-%d %H:%M"),
           "Объект ID": d["obj_id"], "Объект": d["obj_name"], "Автор ID": user.telegram_id,
           "Автор": user.name, "Роль": user.role, "Тип": d["ntype"], "Текст": d["text"],
           "Фото (ссылки)": _links(d["files"]), "Файлы (file_id)": _file_ids(d["files"])}
    if not await _save(call, state, db, "Записи", row):
        return
    await _finish(call, state, f"Запись сохранена ({row['ID']}).")
    if d["ntype"] in NOTIFY_NOTE_TYPES:
        users = await _users_or_empty(db)
        await _notify(bot, users_to_notify(users, NOTIFY_NOTE_ROLES, d["obj_id"], user.telegram_id),
                      f"📝 {d['ntype'].capitalize()} · {d['obj_name']} · {user.name}\n{d['text']}\n"
                      f"{_links(d['files'])}".strip())


# ── Ошибка в боте ─────────────────────────────────────────────────────────────

@menu.message(F.text == kb.BTN_BUG)
@menu.message(Command("bug"))
async def bug_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(Bug.text)
    await message.answer("Что не так? Опишите, что нажимали и что ожидали. Можно голосом.",
                         reply_markup=kb.step())


@router.message(Bug.text)
async def bug_text(message: Message, state: FSMContext, db: Storage, cfg: Config,
                   user: User, bot: Bot) -> None:
    text = await _read_text(message, bot, cfg, "ошибка в боте")
    if not text:
        await message.answer("Напишите текстом или голосовым.")
        return
    now = _now(cfg)
    row = {"ID": new_id("БАГ", now), "Дата": now.strftime("%Y-%m-%d %H:%M"),
           "Автор ID": user.telegram_id, "Автор": user.name, "Текст": text, "Статус": "новая"}
    try:
        await db.append("Ошибки", row)
    except Exception:
        log.exception("ошибка записи бага")
        await message.answer("Не получилось сохранить — попробуйте ещё раз через минуту.")
        return
    await _to_menu(message, state, "Спасибо! Записали, исправим.")
    await _notify_admins(bot, cfg, await _users_or_empty(db), f"🐞 {user.name}: {text}")


# ── Расход ────────────────────────────────────────────────────────────────────
# Вечерний столбик прораба: «Люди 12000 нал», «Заборная сетка 75 м 9900 нал».

@menu.message(F.text == kb.BTN_EXPENSE)
async def expense_start(message: Message, state: FSMContext, db: Storage, user: User) -> None:
    await _start_flow(message, state, db, user, "expense")


@menu.callback_query(F.data.startswith("exp_more:"))
async def expense_more(call: CallbackQuery, state: FSMContext, db: Storage, user: User) -> None:
    """«➕ Ещё расход» — сразу следующий расход по тому же объекту."""
    await _close_buttons(call)
    await _start_flow(call.message, state, db, user, "expense", obj_id=call.data.split(":", 1)[1])


@router.callback_query(Expense.article, F.data.startswith("ea:"))
async def expense_article(call: CallbackQuery, state: FSMContext) -> None:
    payload = await _arg(call, state)
    article = _pick(EXPENSE_ARTICLES, payload) if payload is not None else None
    if article is None:
        return
    await _close_buttons(call, f"Статья: {article}")
    await state.update_data(article=article)
    await state.set_state(Expense.what)
    await call.message.answer("Что и сколько стоит? Одной строкой, как в чате:\n"
                              "«Заборная сетка 75 м 9900 нал», «Люди 12000 нал (не оплатил)».",
                              reply_markup=kb.step())


@router.message(Expense.what, F.text)
async def expense_what(message: Message, state: FSMContext) -> None:
    try:
        a = parse_amount(message.text)
    except InputError as e:
        await message.answer(str(e))
        return
    await state.update_data(what=a.what, amount=a.amount, method=a.method or "",
                            paid=None if not a.unpaid else False)
    if not a.method:
        await _ask_method(message, state)
    elif a.unpaid:
        await _ask_expense_photos(message, state)
    else:
        await _ask_paid(message, state)


async def _ask_method(message: Message, state: FSMContext) -> None:
    await state.set_state(Expense.method)
    nonce = (await state.get_data())["nonce"]
    await message.answer("Как платили?", reply_markup=kb.inline(
        [(m.capitalize(), _cb("em", nonce, str(i))) for i, m in enumerate(PAY_METHODS)], per_row=3))


async def _ask_paid(message: Message, state: FSMContext) -> None:
    await state.set_state(Expense.paid)
    nonce = (await state.get_data())["nonce"]
    await message.answer("Уже оплачено?", reply_markup=kb.inline(
        [("Оплачено", _cb("ep", nonce, "1")), ("Долг, не оплачено", _cb("ep", nonce, "0"))], per_row=2))


async def _ask_expense_photos(message: Message, state: FSMContext) -> None:
    await state.set_state(Expense.photos)
    await message.answer("Фото чека или накладной, затем «Готово».", reply_markup=kb.step(kb.BTN_NO_PHOTO))


@router.callback_query(Expense.method, F.data.startswith("em:"))
async def expense_method(call: CallbackQuery, state: FSMContext) -> None:
    payload = await _arg(call, state)
    method = _pick(PAY_METHODS, payload) if payload is not None else None
    if method is None:
        return
    await _close_buttons(call, f"Оплата: {method}")
    await state.update_data(method=method)
    if (await state.get_data()).get("paid") is False:
        await _ask_expense_photos(call.message, state)
    else:
        await _ask_paid(call.message, state)


@router.callback_query(Expense.paid, F.data.startswith("ep:"))
async def expense_paid(call: CallbackQuery, state: FSMContext) -> None:
    payload = await _arg(call, state)
    if payload not in {"0", "1"}:
        return
    await _close_buttons(call, "Оплачено" if payload == "1" else "Долг, не оплачено")
    await state.update_data(paid=payload == "1")
    await _ask_expense_photos(call.message, state)


@router.message(Expense.photos, MEDIA)
async def expense_photo(message: Message, state: FSMContext, bot: Bot, cfg: Config, user: User) -> None:
    await _collect_media(message, state, bot, cfg, user)


@router.message(Expense.photos, F.text.in_({kb.BTN_DONE, kb.BTN_NO_PHOTO}))
async def expense_photos_done(message: Message, state: FSMContext, cfg: Config) -> None:
    d = await state.get_data()
    await state.set_state(Expense.confirm)
    await _ask_confirm(message, state, cfg, "РС",
                       f"💸 Расход · {d['obj_name']}\n"
                       f"Статья: {d['article']}\n"
                       f"Что: {d['what']}\n"
                       f"Сумма: {money(d['amount'])}, {d['method']}\n"
                       f"{'Оплачено' if d['paid'] else 'Долг, не оплачено'}\n"
                       f"Фото: {len(d['files'])}")


@router.callback_query(Expense.confirm, F.data.startswith("save:"))
async def expense_save(call: CallbackQuery, state: FSMContext, db: Storage, cfg: Config,
                       user: User) -> None:
    if await _arg(call, state) is None:
        return
    d = await state.get_data()
    row = {"ID": d["row_id"], "Дата": _now(cfg).strftime("%Y-%m-%d %H:%M"),
           "Объект ID": d["obj_id"], "Объект": d["obj_name"], "Автор ID": user.telegram_id,
           "Автор": user.name, "Роль": user.role, "Статья": d["article"], "Что": d["what"],
           "Сумма": d["amount"], "Способ оплаты": d["method"], "Оплачено": "да" if d["paid"] else "нет",
           "Фото (ссылки)": _links(d["files"]), "Файлы (file_id)": _file_ids(d["files"])}
    if not await _save(call, state, db, "Расходы", row):
        return
    balance = ""
    if d["method"] == "наличные" and d["paid"]:
        balance = await _balance_line(db, user.telegram_id)
    await state.clear()
    await _close_buttons(call)
    await call.message.answer(f"Расход сохранён ({row['ID']}).{balance}", reply_markup=kb.main_menu())
    await call.message.answer("Ещё расход по этому объекту?", reply_markup=kb.inline(
        [("➕ Ещё расход", f"exp_more:{d['obj_id']}")]))


async def _balance_line(db: Storage, user_id: int) -> str:
    try:
        b = cash_balance(user_id, await db.rows("Подотчёт"), await db.rows("Расходы"))
    except StorageUnavailable:
        return ""
    return f"\nОстаток подотчёта: {money(b)}" + (" — перерасход" if b < 0 else "")


# ── Подотчёт: выдал деньги и остаток ──────────────────────────────────────────

@menu.message(F.text == kb.BTN_BALANCE)
@menu.message(Command("balance"))
async def balance(message: Message, db: Storage, user: User) -> None:
    """Свой остаток; директору, помощнику и бухгалтеру — остатки всех."""
    try:
        issued, expenses, users = await db.rows("Подотчёт"), await db.rows("Расходы"), await db.list_users()
    except StorageUnavailable:
        await message.answer(UNAVAILABLE)
        return
    if user.role in CASH_ISSUER_ROLES:
        lines = []
        for u in users:
            if u.active and u.role in CASH_RECEIVER_ROLES:
                b = cash_balance(u.telegram_id, issued, expenses)
                if b:
                    lines.append(f"{u.name}: {money(b)}")
        text = "Остатки подотчёта:\n" + ("\n".join(lines) if lines else "ни у кого нет")
    else:
        text = f"Ваш остаток подотчёта: {money(cash_balance(user.telegram_id, issued, expenses))}"
    await message.answer(text, reply_markup=kb.main_menu())


@menu.message(F.text == kb.BTN_CASH)
async def cash_start(message: Message, state: FSMContext, db: Storage, user: User) -> None:
    await state.clear()
    if user.role not in CASH_ISSUER_ROLES:
        await message.answer("Выдачу денег записывают директор, помощник и бухгалтер.",
                             reply_markup=kb.main_menu())
        return
    try:
        people = [u for u in await db.list_users()
                  if u.active and u.role in CASH_RECEIVER_ROLES and u.telegram_id != user.telegram_id]
    except StorageUnavailable:
        await message.answer(UNAVAILABLE, reply_markup=kb.main_menu())
        return
    if not people:
        await message.answer("Некому выдавать: в таблице нет активных прорабов и РП.",
                             reply_markup=kb.main_menu())
        return
    nonce = secrets.token_hex(3)
    await state.update_data(nonce=nonce, flow="cash", files=[],
                            people=[(u.telegram_id, u.name, sorted(u.objects), u.sees_all) for u in people])
    await state.set_state(Cash.who)
    await message.answer("Кому выдали деньги?", reply_markup=kb.step())
    await message.answer("Получатель:", reply_markup=kb.inline(
        [(f"{u.name} ({u.role})", _cb("cw", nonce, str(i))) for i, u in enumerate(people)]))


@router.callback_query(Cash.who, F.data.startswith("cw:"))
async def cash_who(call: CallbackQuery, state: FSMContext) -> None:
    payload = await _arg(call, state)
    picked = _pick((await state.get_data())["people"], payload) if payload is not None else None
    if picked is None:
        return
    tid, name, objs, sees_all = picked
    await state.update_data(to_id=tid, to_name=name, to_objs=objs, to_all=sees_all)
    await _close_buttons(call, f"Кому: {name}")
    await state.set_state(Cash.amount)
    await call.message.answer("Сколько? Например 50000 или 50к.", reply_markup=kb.step())


@router.message(Cash.amount, F.text)
async def cash_amount(message: Message, state: FSMContext, db: Storage) -> None:
    try:
        amount = parse_money(message.text)
    except InputError as e:
        await message.answer(str(e))
        return
    await state.update_data(amount=amount)
    d = await state.get_data()
    try:
        all_objs = [o for o in await db.list_objects() if o.active]
    except StorageUnavailable:
        all_objs = []
    objs = all_objs if d["to_all"] else [o for o in all_objs if o.id in d["to_objs"]]
    await state.update_data(cash_objs=[(o.id, o.name) for o in objs])
    await state.set_state(Cash.obj)
    opts = [(o.name, _cb("co", d["nonce"], str(i))) for i, o in enumerate(objs)]
    opts.append(("Без объекта", _cb("co", d["nonce"], "-")))
    await message.answer("На какой объект?", reply_markup=kb.inline(opts))


@router.callback_query(Cash.obj, F.data.startswith("co:"))
async def cash_obj(call: CallbackQuery, state: FSMContext) -> None:
    payload = await _arg(call, state)
    if payload is None:
        return
    if payload == "-":
        oid, name = "", ""
    else:
        picked = _pick((await state.get_data())["cash_objs"], payload)
        if picked is None:
            return
        oid, name = picked
    await state.update_data(obj_id=oid, obj_name=name)
    await _close_buttons(call, f"Объект: {name or 'без объекта'}")
    await state.set_state(Cash.purpose)
    await call.message.answer("На что? Например «песок», «зарплата бригаде».",
                              reply_markup=kb.step(kb.BTN_SKIP))


@router.message(Cash.purpose, F.text)
async def cash_purpose(message: Message, state: FSMContext, cfg: Config) -> None:
    await state.update_data(purpose="" if message.text == kb.BTN_SKIP else message.text.strip())
    d = await state.get_data()
    await state.set_state(Cash.confirm)
    await _ask_confirm(message, state, cfg, "ПО",
                       f"💰 Выдано под отчёт\nКому: {d['to_name']}\nСумма: {money(d['amount'])}\n"
                       f"Объект: {d['obj_name'] or '—'}\nНа что: {d['purpose'] or '—'}")


@router.callback_query(Cash.confirm, F.data.startswith("save:"))
async def cash_save(call: CallbackQuery, state: FSMContext, db: Storage, cfg: Config,
                    user: User, bot: Bot) -> None:
    if await _arg(call, state) is None:
        return
    d = await state.get_data()
    row = {"ID": d["row_id"], "Дата": _now(cfg).strftime("%Y-%m-%d %H:%M"), "Кому ID": d["to_id"],
           "Кому": d["to_name"], "Объект ID": d["obj_id"], "Объект": d["obj_name"],
           "Сумма": d["amount"], "На что": d["purpose"], "Выдал ID": user.telegram_id, "Выдал": user.name}
    if not await _save(call, state, db, "Подотчёт", row):
        return
    balance_line = await _balance_line(db, d["to_id"])
    await _finish(call, state, f"Записано ({row['ID']}). {d['to_name']}{balance_line.replace(chr(10), ': ', 1)}")
    await _send(bot, d["to_id"], f"💰 {user.name} выдал вам {money(d['amount'])}"
                                 f"{' на ' + d['purpose'] if d['purpose'] else ''}"
                                 f"{' · ' + d['obj_name'] if d['obj_name'] else ''}.{balance_line}")


# ── Счёт на оплату ────────────────────────────────────────────────────────────
# Прораб или снабжение прикладывает счёт → директор согласует → бухгалтер оплачивает.
# Кнопки в уведомлении работают без сценария: ID счёта в callback_data.

@menu.message(F.text == kb.BTN_INVOICE)
async def invoice_start(message: Message, state: FSMContext, db: Storage, user: User) -> None:
    await _start_flow(message, state, db, user, "invoice", roles=INVOICE_ROLES)


@router.message(Invoice.file, MEDIA)
async def invoice_file(message: Message, state: FSMContext, bot: Bot, cfg: Config, user: User) -> None:
    await _collect_media(message, state, bot, cfg, user)
    d = await state.get_data()
    if len(d["files"]) == 1:
        await state.set_state(Invoice.what)
        await message.answer("За что и сумма? «Дорнит 2 рулона 18500».", reply_markup=kb.step())


@router.message(Invoice.what, MEDIA)
async def invoice_more_files(message: Message, state: FSMContext, bot: Bot, cfg: Config, user: User) -> None:
    await _collect_media(message, state, bot, cfg, user)
    await message.answer("Теперь напишите, за что и сумму.")


@router.message(Invoice.what, F.text)
async def invoice_what(message: Message, state: FSMContext, cfg: Config) -> None:
    if message.text == kb.BTN_DONE:
        await message.answer("Напишите, за что и сумму: «Дорнит 2 рулона 18500».")
        return
    try:
        a = parse_amount(message.text)
    except InputError as e:
        await message.answer(str(e))
        return
    await state.update_data(what=a.what, amount=a.amount)
    d = await state.get_data()
    await state.set_state(Invoice.confirm)
    await _ask_confirm(message, state, cfg, "СЧ",
                       f"🧾 Счёт · {d['obj_name']}\nЗа что: {a.what}\nСумма: {money(a.amount)}\n"
                       f"Файлов: {len(d['files'])}")


def _invoice_buttons(inv_id: str, status: str):
    opts = []
    if status == INVOICE_NEW:
        opts += [("👍 Согласовать", f"inv:a:{inv_id}"), ("✖ Отклонить", f"inv:r:{inv_id}")]
    if status in {INVOICE_NEW, INVOICE_APPROVED}:
        opts.append(("✅ Оплачено", f"inv:p:{inv_id}"))
    return kb.inline(opts, per_row=2) if opts else None


@router.callback_query(Invoice.confirm, F.data.startswith("save:"))
async def invoice_save(call: CallbackQuery, state: FSMContext, db: Storage, cfg: Config,
                       user: User, bot: Bot) -> None:
    if await _arg(call, state) is None:
        return
    d = await state.get_data()
    row = {"ID": d["row_id"], "Создан": _now(cfg).strftime("%Y-%m-%d %H:%M"), "Объект ID": d["obj_id"],
           "Объект": d["obj_name"], "Автор ID": user.telegram_id, "Автор": user.name,
           "За что": d["what"], "Сумма": d["amount"], "Файлы (ссылки)": _links(d["files"]),
           "Файлы (file_id)": _file_ids(d["files"]), "Статус": INVOICE_NEW}
    if not await _save(call, state, db, "Счета", row):
        return
    await _finish(call, state, f"Счёт {row['ID']} отправлен на согласование. Сообщу, когда оплатят.")
    text = (f"🧾 Счёт {row['ID']} · {d['obj_name']}\nОт: {user.name}\nЗа что: {d['what']}\n"
            f"Сумма: {money(d['amount'])}\n{_links(d['files'])}").strip()
    for u in users_to_notify(await _users_or_empty(db), NOTIFY_INVOICE_ROLES, d["obj_id"], user.telegram_id):
        try:
            await bot.send_message(u.telegram_id, _clip(text), reply_markup=_invoice_buttons(row["ID"], INVOICE_NEW))
        except Exception as e:
            log.warning("не отправлено %s: %s", u.telegram_id, e)


@menu.callback_query(F.data.startswith("inv:"))
async def invoice_action(call: CallbackQuery, db: Storage, cfg: Config, user: User, bot: Bot) -> None:
    _, action, inv_id = call.data.split(":", 2)
    allowed = {"a": APPROVE_ROLES, "r": APPROVE_ROLES, "p": PAY_ROLES}.get(action, set())
    if user.role not in allowed:
        await call.message.answer("Согласует директор, отмечает оплату бухгалтер или директор.")
        return
    try:
        inv = next((r for r in await db.rows("Счета") if r.get("ID") == inv_id), None)
    except StorageUnavailable:
        await call.message.answer(UNAVAILABLE)
        return
    if inv is None:
        await call.message.answer("Счёт не найден в таблице.")
        return
    status = str(inv.get("Статус", ""))
    new_status = {"a": INVOICE_APPROVED, "r": INVOICE_REJECTED, "p": INVOICE_PAID}[action]
    if status in {INVOICE_PAID, INVOICE_REJECTED} or (action == "a" and status != INVOICE_NEW):
        await call.message.answer(f"Счёт уже: {status}.")
        await _close_buttons(call)
        return
    stamp = f"{user.name}, {_now(cfg):%Y-%m-%d %H:%M}"
    fields = {"Статус": new_status}
    if action in {"a", "r"}:
        fields["Согласовал"] = stamp
    else:
        fields.update({"Оплатил": user.name, "Дата оплаты": _now(cfg).strftime("%Y-%m-%d")})
    try:
        await db.update("Счета", "ID", inv_id, fields)
    except StorageUnavailable:
        await call.message.answer(UNAVAILABLE)
        return
    try:
        await call.message.edit_text(_clip(f"{call.message.text or ''}\n\n➡️ {new_status} — {stamp}"),
                                     reply_markup=_invoice_buttons(inv_id, new_status))
    except Exception as e:
        log.debug("не отредактировано: %s", e)
    summary = f"🧾 Счёт {inv_id} ({inv.get('За что', '')}, {money(float(inv.get('Сумма') or 0))}): {new_status} — {user.name}."
    author_id = inv.get("Автор ID")
    if author_id and str(author_id) != str(user.telegram_id):
        await _send(bot, int(author_id), summary)
    if action == "a":                        # согласовали → бухгалтеру к оплате
        for u in await _users_or_empty(db):
            if u.active and u.role == "бухгалтер" and u.telegram_id != user.telegram_id:
                try:
                    await bot.send_message(u.telegram_id, _clip(f"К оплате: {summary}\n{inv.get('Файлы (ссылки)', '')}"),
                                           reply_markup=_invoice_buttons(inv_id, INVOICE_APPROVED))
                except Exception as e:
                    log.warning("не отправлено %s: %s", u.telegram_id, e)


# ── Приглашения: человек сам попадает в таблицу с ролью и объектами ───────────

INVITE_DAYS = 7
_bot_username = ""


async def _username(bot: Bot) -> str:
    global _bot_username
    if not _bot_username:
        _bot_username = (await bot.get_me()).username or ""
    return _bot_username


def _invite_code(event) -> str:
    text = getattr(event, "text", None) or ""
    m = text.split()
    if len(m) == 2 and m[0].split("@")[0] == "/start" and m[1].startswith("inv_"):
        return m[1][4:]
    return ""


async def _accept_invite(code: str, tg_user, user: User | None, db: Storage, cfg: Config,
                         bot: Bot) -> str:
    """Проверить код, добавить или включить пользователя. Возвращает текст ответа."""
    inv = next((r for r in await db.rows("Приглашения") if str(r.get("Код")) == code), None)
    if inv is None:
        return "Ссылка-приглашение не найдена. Попросите новую."
    if str(inv.get("Использовал ID", "")).strip():
        return "Эта ссылка уже использована. Попросите новую."
    try:
        created = datetime.fromisoformat(str(inv.get("Создано")))
        if created.tzinfo is None:
            created = created.replace(tzinfo=ZoneInfo(cfg.tz))
        if _now(cfg) - created > timedelta(days=INVITE_DAYS):
            return "Срок ссылки истёк. Попросите новую."
    except ValueError:
        return "Ссылка повреждена. Попросите новую."
    name = " ".join(filter(None, [tg_user.first_name, tg_user.last_name])) or str(tg_user.id)
    role, objs = str(inv.get("Роль")), str(inv.get("Объекты", ""))
    now = _now(cfg).strftime("%Y-%m-%d %H:%M")
    if user is None:
        await db.append("Пользователи", {"Telegram ID": tg_user.id, "ФИО": name, "Роль": role,
                                         "Объекты": objs, "Активен": "да",
                                         "Username": tg_user.username or "", "Добавлен": now,
                                         "Комментарий": f"по приглашению {code}"})
    else:
        await db.update("Пользователи", "Telegram ID", tg_user.id,
                        {"Роль": role, "Объекты": objs, "Активен": "да"})
    await db.update("Приглашения", "Код", code,
                    {"Использовал ID": tg_user.id, "Использовал": name, "Использовано": now})
    kb.CURRENT_ROLE.set(role)
    try:
        await _send(bot, int(inv.get("Создал ID")), f"✅ {name} принял приглашение: {role}, объекты: {objs}.")
    except (TypeError, ValueError):
        pass
    return f"Готово, {name}! Вы в системе как «{role}».\n\n{HELP}"


@menu.message(F.text == kb.BTN_INVITE)
async def invite_start(message: Message, state: FSMContext, user: User) -> None:
    await state.clear()
    if user.role not in INVITE_ROLES:
        await message.answer("Приглашать могут директор и помощник.", reply_markup=kb.main_menu())
        return
    nonce = secrets.token_hex(3)
    roles = ["прораб", "бригадир", "рп", "снабжение", "помощник", "экономист", "бухгалтер", "геодезист"]
    await state.update_data(nonce=nonce, roles=roles, picked=[])
    await state.set_state(Invite.role)
    await message.answer("Кого приглашаем?", reply_markup=kb.step())
    await message.answer("Роль:", reply_markup=kb.inline(
        [(r, _cb("ir", nonce, str(i))) for i, r in enumerate(roles)], per_row=3))


@router.callback_query(Invite.role, F.data.startswith("ir:"))
async def invite_role(call: CallbackQuery, state: FSMContext, db: Storage, cfg: Config,
                      user: User, bot: Bot) -> None:
    payload = await _arg(call, state)
    d = await state.get_data()
    role = _pick(d["roles"], payload) if payload is not None else None
    if role is None:
        return
    await state.update_data(role=role)
    await _close_buttons(call, f"Роль: {role}")
    if role in ALL_OBJECTS_ROLES:            # видят все объекты — выбирать нечего
        await _make_invite(call.message, state, db, cfg, user, bot, "все")
        return
    try:
        objs = [o for o in await db.list_objects() if o.active]
    except StorageUnavailable:
        await call.message.answer(UNAVAILABLE)
        return
    await state.update_data(inv_objs=[(o.id, o.name) for o in objs])
    await state.set_state(Invite.objects)
    await call.message.answer("Отметьте объекты (можно несколько), затем «Готово».",
                              reply_markup=_invite_objects_kb(await state.get_data()))


def _invite_objects_kb(d: dict):
    opts = [(("☑️ " if oid in d["picked"] else "⬜ ") + name, _cb("io", d["nonce"], str(i)))
            for i, (oid, name) in enumerate(d["inv_objs"])]
    opts += [("Все объекты", _cb("io", d["nonce"], "all")), ("Готово", _cb("io", d["nonce"], "done"))]
    return kb.inline(opts)


@router.callback_query(Invite.objects, F.data.startswith("io:"))
async def invite_objects(call: CallbackQuery, state: FSMContext, db: Storage, cfg: Config,
                         user: User, bot: Bot) -> None:
    payload = await _arg(call, state)
    if payload is None:
        return
    d = await state.get_data()
    if payload == "all":
        await _close_buttons(call, "Объекты: все")
        await _make_invite(call.message, state, db, cfg, user, bot, "все")
        return
    if payload == "done":
        if not d["picked"]:
            await call.message.answer("Отметьте хотя бы один объект или нажмите «Все объекты».")
            return
        await _close_buttons(call, "Объекты: " + ", ".join(d["picked"]))
        await _make_invite(call.message, state, db, cfg, user, bot, ", ".join(d["picked"]))
        return
    picked_obj = _pick(d["inv_objs"], payload)
    if picked_obj is None:
        return
    picked = set(d["picked"]) ^ {picked_obj[0]}
    await state.update_data(picked=[oid for oid, _ in d["inv_objs"] if oid in picked])
    try:
        await call.message.edit_reply_markup(reply_markup=_invite_objects_kb(await state.get_data()))
    except Exception as e:
        log.debug("не отредактировано: %s", e)


async def _make_invite(message: Message, state: FSMContext, db: Storage, cfg: Config, user: User,
                       bot: Bot, objects: str) -> None:
    d = await state.get_data()
    code = secrets.token_urlsafe(9)
    try:
        await db.append("Приглашения", {"Код": code, "Роль": d["role"], "Объекты": objects,
                                        "Создал ID": user.telegram_id, "Создал": user.name,
                                        "Создано": _now(cfg).isoformat(timespec="seconds")})
    except StorageUnavailable:
        await _to_menu(message, state, UNAVAILABLE)
        return
    link = f"https://t.me/{await _username(bot)}?start=inv_{code}"
    await _to_menu(message, state,
                   f"Перешлите эту ссылку человеку ({d['role']}, объекты: {objects}):\n\n{link}\n\n"
                   f"Он нажмёт «Запустить» — и сразу попадёт в систему, ничего вписывать в таблицу "
                   f"не нужно. Ссылка одноразовая, действует {INVITE_DAYS} дней.")


# ── Всё остальное ─────────────────────────────────────────────────────────────

@router.message(StateFilter(None))
async def fallback_idle(message: Message) -> None:
    await message.answer("Выберите действие кнопкой внизу.", reply_markup=kb.main_menu())


@router.message()
async def fallback_in_flow(message: Message) -> None:
    await message.answer(f"Не понял ответ. Ответьте на вопрос выше или нажмите «{kb.BTN_CANCEL}».")


@router.callback_query()
async def fallback_callback(call: CallbackQuery) -> None:
    await call.message.answer("Эта кнопка устарела. Начните заново из меню.")

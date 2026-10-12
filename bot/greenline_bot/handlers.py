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
from .domain import (DELIVERY_FULL, DELIVERY_PART, NOTE_TYPES, NOTIFY_NOTE_ROLES,
                     NOTIFY_NOTE_TYPES, NOTIFY_REQUEST_ROLES, REQUEST_STATUS_NEW,
                     REQUEST_TYPES, ROLES, SENDER_ROLES, InputError, User,
                     archive_link, due_date_ok, new_id, objects_for, parse_due_date,
                     parse_machinery, parse_people, users_to_notify)
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
                return await handler(event, data)
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
                      flow: str) -> None:
    await state.clear()
    if not _can_send(user):
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
    if len(objs) == 1:
        await state.update_data(obj_id=objs[0].id, obj_name=objs[0].name)
        await message.answer(f"Объект: {objs[0].name}")
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
    await message.answer("Техника и часы через запятую. Пример: «экскаватор 8, кран 4».\n"
                         "Если техники не было — «Нет».", reply_markup=kb.step(kb.BTN_NO))


@router.message(Report.machinery)
async def report_machinery(message: Message, state: FSMContext) -> None:
    if not message.text:
        await message.answer("Технику и часы — текстом, чтобы посчитать машино-часы. "
                             "Пример: «экскаватор 8, кран 4».")
        return
    try:
        items, total = parse_machinery(message.text)
    except InputError as e:
        await message.answer(str(e))
        return
    raw = ", ".join(f"{n} {h:g}" for n, h in items)
    await state.update_data(machinery=raw, machine_hours=total)
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
                       f"Техника: {d['machinery'] or 'нет'} (всего {d['machine_hours']:g} маш.-ч)\n"
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
           "Техника (как написано)": d["machinery"], "Машино-часы": d["machine_hours"],
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
    await state.set_state(Accept.comment)
    await call.message.answer("Комментарий: брак, недовоз, пересорт? Если нет — «Нет».",
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
    await _ask_confirm(message, state, cfg, "ПР",
                       f"✅ Приёмка · {d['obj_name']}\n"
                       f"Заявка: {d['req_id'] or 'без заявки'}\n"
                       f"Что: {d['what']}\n"
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
           "Полностью / частично": delivery, "Комментарий": d["comment"],
           "Фото (ссылки)": _links(d["files"]), "Файлы (file_id)": _file_ids(d["files"])}
    if not await _save(call, state, db, "Приёмка", row):
        return
    await _finish(call, state, f"Приёмка сохранена ({row['ID']}).")
    if d["req_id"]:
        try:
            await db.set_request_delivery(d["req_id"], delivery)
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
    for tid in cfg.admin_ids:
        await _send(bot, tid, f"🐞 {user.name}: {text}")


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

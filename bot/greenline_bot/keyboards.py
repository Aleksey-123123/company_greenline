"""Клавиатуры. Тексты кнопок — константы: по ним же ловим нажатия."""

from __future__ import annotations

from contextvars import ContextVar

from aiogram.types import (InlineKeyboardButton, InlineKeyboardMarkup,
                           KeyboardButton, ReplyKeyboardMarkup)

# Роль текущего пользователя: ставит AccessMiddleware, читает main_menu().
CURRENT_ROLE: ContextVar[str] = ContextVar("current_role", default="")

BTN_REPORT = "📋 Отчёт за день"
BTN_REQUEST = "🧱 Заявка"
BTN_ACCEPT = "✅ Приёмка"
BTN_NOTE = "📝 Запись по объекту"
BTN_BUG = "🐞 Ошибка в боте"
BTN_EXPENSE = "💸 Расход"
BTN_INVOICE = "🧾 Счёт на оплату"
BTN_CASH = "💰 Выдал деньги"
BTN_INVITE = "🔗 Пригласить"
BTN_BALANCE = "💼 Подотчёт"
BTN_CANCEL = "❌ Отмена"
BTN_SKIP = "Пропустить"
BTN_NO = "Нет"
BTN_DONE = "Готово"
BTN_NO_PHOTO = "Без фото"


SENDERS = {"прораб", "бригадир", "рп", "директор", "помощник"}


def main_menu(role: str | None = None) -> ReplyKeyboardMarkup:
    """Меню по роли: каждый видит только то, что ему можно."""
    role = role if role is not None else CURRENT_ROLE.get()
    rows: list[list[str]] = []
    if role in SENDERS:
        rows += [[BTN_REPORT, BTN_EXPENSE], [BTN_REQUEST, BTN_ACCEPT], [BTN_INVOICE, BTN_NOTE]]
    elif role == "снабжение":
        rows += [[BTN_INVOICE]]
    if role in {"директор", "помощник"}:
        rows += [[BTN_CASH, BTN_INVITE]]
    elif role == "бухгалтер":
        rows += [[BTN_CASH]]
    rows += [[BTN_BALANCE, BTN_BUG]]
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=t) for t in r] for r in rows],
                               resize_keyboard=True, is_persistent=True)


def step(*buttons: str) -> ReplyKeyboardMarkup:
    """Клавиатура шага: кнопки шага + «Отмена» последней строкой."""
    rows = [[KeyboardButton(text=b)] for b in buttons]
    rows.append([KeyboardButton(text=BTN_CANCEL)])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def inline(options: list[tuple[str, str]], per_row: int = 1) -> InlineKeyboardMarkup:
    """options: [(текст, callback_data)]."""
    rows, row = [], []
    for text, data in options:
        row.append(InlineKeyboardButton(text=text, callback_data=data))
        if len(row) == per_row:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def confirm() -> InlineKeyboardMarkup:
    return inline([("✅ Сохранить", "save"), ("❌ Отменить", "drop")], per_row=2)

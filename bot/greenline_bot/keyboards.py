"""Клавиатуры. Тексты кнопок — константы: по ним же ловим нажатия."""

from __future__ import annotations

from aiogram.types import (InlineKeyboardButton, InlineKeyboardMarkup,
                           KeyboardButton, ReplyKeyboardMarkup)

BTN_REPORT = "📋 Отчёт за день"
BTN_REQUEST = "🧱 Заявка"
BTN_ACCEPT = "✅ Приёмка"
BTN_NOTE = "📝 Запись по объекту"
BTN_BUG = "🐞 Ошибка в боте"
BTN_CANCEL = "❌ Отмена"
BTN_SKIP = "Пропустить"
BTN_NO = "Нет"
BTN_DONE = "Готово"
BTN_NO_PHOTO = "Без фото"


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=BTN_REPORT), KeyboardButton(text=BTN_REQUEST)],
                  [KeyboardButton(text=BTN_ACCEPT), KeyboardButton(text=BTN_NOTE)],
                  [KeyboardButton(text=BTN_BUG)]],
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

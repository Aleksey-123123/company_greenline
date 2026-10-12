"""Сквозные сценарии: апдейты идут через настоящий Dispatcher, Telegram API подменён."""

import asyncio
import importlib
import itertools
import unittest
from datetime import datetime, timedelta

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import (AnswerCallbackQuery, CopyMessage, DeleteWebhook,
                             EditMessageReplyMarkup, EditMessageText, SendMessage)
from aiogram.types import (CallbackQuery, Chat, InlineKeyboardMarkup, Message, MessageId,
                           PhotoSize, Update, User as TgUser, Voice)

import greenline_bot.handlers as handlers
from greenline_bot.__main__ import build_dispatcher
from greenline_bot.config import Config
from greenline_bot.storage import MemoryStorage

FOREMAN, SUPPLY, DIRECTOR, STRANGER = 101, 202, 303, 404
ARCHIVE = -1009999


class FakeSession(BaseSession):
    """Запоминает все вызовы API и отвечает правдоподобными объектами."""

    def __init__(self):
        super().__init__()
        self.calls = []
        self.ids = itertools.count(1000)

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if isinstance(method, (SendMessage, EditMessageText, EditMessageReplyMarkup)):
            chat_id = getattr(method, "chat_id", None) or 1
            return Message(message_id=next(self.ids), date=datetime.now(),
                           chat=Chat(id=chat_id, type="private"),
                           text=getattr(method, "text", None) or "")
        if isinstance(method, CopyMessage):
            return MessageId(message_id=next(self.ids))
        if isinstance(method, (AnswerCallbackQuery, DeleteWebhook)):
            return True
        raise AssertionError(f"неожиданный вызов {type(method).__name__}")

    async def stream_content(self, *a, **kw):  # pragma: no cover
        raise NotImplementedError

    async def close(self):
        pass

    def texts_to(self, chat_id):
        return [c.text for c in self.calls if isinstance(c, SendMessage) and c.chat_id == chat_id]


class Harness:
    def __init__(self):
        self.db = MemoryStorage()
        s = self.db.sheets
        s["Объекты"] += [{"ID": "О-01", "Название": "Мост", "Активен": "да"},
                         {"ID": "О-02", "Название": "Стена", "Активен": ""},
                         {"ID": "О-03", "Название": "Старый", "Активен": "нет"}]
        s["Пользователи"] += [
            {"Telegram ID": FOREMAN, "ФИО": "Прораб Пётр", "Роль": "прораб", "Объекты": "О-01, О-02", "Активен": "да"},
            {"Telegram ID": SUPPLY, "ФИО": "Снабженец", "Роль": "снабжение", "Объекты": "", "Активен": "да"},
            {"Telegram ID": DIRECTOR, "ФИО": "Директор", "Роль": "директор", "Объекты": "", "Активен": "да"},
        ]
        self.cfg = Config(bot_token="1:x", storage="memory", sheet_id="", google_credentials="",
                          archive_chat_id=ARCHIVE, admin_ids=frozenset({DIRECTOR}), tz="Asia/Novosibirsk")
        self.session = FakeSession()
        self.bot = Bot("123:abc", session=self.session)
        self.dp = build_dispatcher(self.db, self.cfg, importlib.reload(handlers).routers)
        self.uid = itertools.count(1)
        self.mid = itertools.count(1)

    def _user(self, uid):
        return TgUser(id=uid, is_bot=False, first_name=f"U{uid}")

    def _msg(self, uid, **kw):
        return Message(message_id=next(self.mid), date=datetime.now(),
                       chat=Chat(id=uid, type="private"), from_user=self._user(uid), **kw)

    async def send(self, uid, text=None, **kw):
        upd = Update(update_id=next(self.uid), message=self._msg(uid, text=text, **kw))
        await self.dp.feed_update(self.bot, upd)

    async def photo(self, uid):
        await self.send(uid, photo=[PhotoSize(file_id=f"ph{next(self.mid)}", file_unique_id="u",
                                              width=10, height=10)])

    async def voice(self, uid):
        await self.send(uid, voice=Voice(file_id="vc1", file_unique_id="v", duration=3))

    def button(self, uid, kind, payload=""):
        """callback_data последней кнопки вида kind:<nonce>:payload, отправленной uid."""
        for c in reversed(self.session.calls):
            if isinstance(c, SendMessage) and c.chat_id == uid and isinstance(c.reply_markup, InlineKeyboardMarkup):
                for row in c.reply_markup.inline_keyboard:
                    for b in row:
                        parts = b.callback_data.split(":", 2)
                        if parts[0] == kind and parts[2] == payload:
                            return b.callback_data
        raise AssertionError(f"нет кнопки {kind}:{payload}")

    async def press(self, uid, kind, payload=""):
        await self.press_raw(uid, self.button(uid, kind, payload))

    async def press_raw(self, uid, data):
        cq = CallbackQuery(id=str(next(self.uid)), from_user=self._user(uid), chat_instance="c",
                           data=data, message=self._msg(uid, text="кнопки"))
        await self.dp.feed_update(self.bot, Update(update_id=next(self.uid), callback_query=cq))


def run(coro):
    return asyncio.run(coro)


class Flows(unittest.TestCase):
    def test_daily_report_with_choice_of_object_and_photos(self):
        async def go():
            h = Harness()
            await h.send(FOREMAN, "/start")
            await h.send(FOREMAN, "📋 Отчёт за день")
            await h.press(FOREMAN, "obj", "1")                   # Стена
            await h.send(FOREMAN, "шесть")                    # ошибка ввода — переспросит
            await h.send(FOREMAN, "6")
            await h.send(FOREMAN, "ООО Бетон")
            await h.send(FOREMAN, "экскаватор 8, кран 4")
            await h.voice(FOREMAN)                            # «сделано» голосом
            await h.send(FOREMAN, "Ждём арматуру, простой 2 ч")
            await h.photo(FOREMAN)
            await h.photo(FOREMAN)
            await h.send(FOREMAN, "Готово")
            await h.press(FOREMAN, "save")
            return h
        h = run(go())
        rows = h.db.sheets["Отчёты"]
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual((r["Объект ID"], r["Людей"], r["Машино-часы"], r["Фото, шт"]), ("О-02", 6, 12.0, 2))
        self.assertEqual(r["Техника (как написано)"], "экскаватор 8, кран 4")
        self.assertTrue(r["Сделано"].startswith("🎤 голосовое: https://t.me/c/9999/"))
        self.assertEqual(len(r["Фото (ссылки)"].splitlines()), 2)
        self.assertIn("Напишите число людей", "\n".join(h.session.texts_to(FOREMAN)))
        # проблема в отчёте → директору уведомление; снабжению — нет
        self.assertTrue(any("Ждём арматуру" in t for t in h.session.texts_to(DIRECTOR)))
        self.assertFalse(any("Ждём арматуру" in t for t in h.session.texts_to(SUPPLY)))

    def test_request_then_acceptance_updates_delivery(self):
        async def go():
            h = Harness()
            await h.send(FOREMAN, "🧱 Заявка")
            await h.press(FOREMAN, "obj", "0")
            await h.press(FOREMAN, "rt", "0")
            await h.send(FOREMAN, "Цемент М500 — 40 мешков")
            await h.send(FOREMAN, "01.01.2020")               # прошлое — переспросит
            await h.press(FOREMAN, "due", (datetime.now().date() + timedelta(days=7)).isoformat())
            await h.press(FOREMAN, "urg", "1")
            await h.send(FOREMAN, "Пропустить")
            await h.send(FOREMAN, "Без фото")
            await h.press(FOREMAN, "save")
            await h.send(FOREMAN, "✅ Приёмка")
            await h.press(FOREMAN, "obj", "0")
            await h.press(FOREMAN, "ar", "0")
            await h.press(FOREMAN, "full", "0")
            await h.send(FOREMAN, "Нет")
            await h.photo(FOREMAN)
            await h.send(FOREMAN, "Готово")
            await h.press(FOREMAN, "save")
            return h
        h = run(go())
        req = h.db.sheets["Заявки"][0]
        due = (datetime.now().date() + timedelta(days=7)).isoformat()
        self.assertEqual((req["Объект ID"], req["Тип"], req["Нужно к"], req["Срочно"], req["Статус"]),
                         ("О-01", "материал", due, "да", "Новая"))
        self.assertEqual(req["Поставка"], "частично")
        acc = h.db.sheets["Приёмка"][0]
        self.assertEqual((acc["Заявка ID"], acc["Полностью / частично"]), (req["ID"], "частично"))
        self.assertTrue(any("Новая заявка" in t for t in h.session.texts_to(SUPPLY)))
        self.assertTrue(any("принял" in t for t in h.session.texts_to(SUPPLY)))

    def test_note_and_bug(self):
        async def go():
            h = Harness()
            await h.send(DIRECTOR, "📝 Запись по объекту")
            await h.press(DIRECTOR, "obj", "0")
            await h.press(DIRECTOR, "nt", "1")                   # допработа
            await h.send(DIRECTOR, "Доп. отсыпка 30 м3 без ДС")
            await h.send(DIRECTOR, "Без фото")
            await h.press(DIRECTOR, "save")
            await h.send(FOREMAN, "🐞 Ошибка в боте")
            await h.send(FOREMAN, "кнопка не работает")
            return h
        h = run(go())
        self.assertEqual(h.db.sheets["Записи"][0]["Тип"], "допработа")
        self.assertEqual(h.db.sheets["Ошибки"][0]["Текст"], "кнопка не работает")
        self.assertTrue(any("🐞" in t for t in h.session.texts_to(DIRECTOR)))

    def test_menu_button_mid_flow_restarts(self):
        async def go():
            h = Harness()
            await h.send(FOREMAN, "📋 Отчёт за день")
            await h.press(FOREMAN, "obj", "0")
            await h.send(FOREMAN, "🧱 Заявка")                # посреди отчёта
            await h.press(FOREMAN, "obj", "0")
            await h.press(FOREMAN, "rt", "2")
            return h
        h = run(go())
        self.assertIn("Что нужно и сколько? Можно голосом.\nПример: «цемент М500 — 40 мешков».",
                      h.session.texts_to(FOREMAN))

    def test_stranger_registered_inactive_and_admin_notified(self):
        async def go():
            h = Harness()
            await h.send(STRANGER, "/start")
            await h.send(STRANGER, "📋 Отчёт за день")
            await h.send(STRANGER, "/myid")
            return h
        h = run(go())
        rec = [r for r in h.db.sheets["Пользователи"] if r["Telegram ID"] == STRANGER]
        self.assertEqual(len(rec), 1)
        self.assertEqual(rec[0]["Активен"], "нет")
        self.assertTrue(any("Новый пользователь" in t for t in h.session.texts_to(DIRECTOR)))
        self.assertIn(f"Ваш Telegram ID: {STRANGER}", h.session.texts_to(STRANGER))
        self.assertEqual(h.db.sheets["Отчёты"], [])

    def test_group_chat_ignored_except_chatid(self):
        async def go():
            h = Harness()
            for text in ["/start", "/chatid"]:
                msg = Message(message_id=next(h.mid), date=datetime.now(),
                              chat=Chat(id=ARCHIVE, type="supergroup"),
                              from_user=h._user(STRANGER), text=text)
                await h.dp.feed_update(h.bot, Update(update_id=next(h.uid), message=msg))
            return h
        h = run(go())
        self.assertEqual(h.session.texts_to(ARCHIVE), [f"ID этого чата: {ARCHIVE}"])
        self.assertFalse(any(r["Telegram ID"] == STRANGER for r in h.db.sheets["Пользователи"]))

    def test_single_object_skips_choice_and_closed_hidden(self):
        async def go():
            h = Harness()
            h.db.sheets["Пользователи"][0]["Объекты"] = "О-01, О-03"   # О-03 закрыт
            await h.send(FOREMAN, "📋 Отчёт за день")
            return h
        h = run(go())
        texts = h.session.texts_to(FOREMAN)
        self.assertIn("Объект: Мост", texts)
        self.assertIn("Сколько людей работало сегодня? Числом.", texts)

    def test_double_save_writes_one_row(self):
        async def go():
            h = Harness()
            await h.send(DIRECTOR, "📝 Запись по объекту")
            await h.press(DIRECTOR, "obj", "0")
            await h.press(DIRECTOR, "nt", "0")
            await h.send(DIRECTOR, "Нет света на площадке")
            await h.send(DIRECTOR, "Без фото")
            real_append = h.db.append

            async def slow(sheet, row):            # как Google: запись идёт по сети
                await asyncio.sleep(0.05)
                await real_append(sheet, row)
            h.db.append = slow
            data = h.button(DIRECTOR, "save")
            await asyncio.gather(h.press_raw(DIRECTOR, data), h.press_raw(DIRECTOR, data))
            return h
        h = run(go())
        self.assertEqual(len(h.db.sheets["Записи"]), 1)

    def test_stale_buttons_from_previous_flow_do_nothing(self):
        async def go():
            h = Harness()
            await h.send(FOREMAN, "📝 Запись по объекту")
            await h.press(FOREMAN, "obj", "0")
            await h.press(FOREMAN, "nt", "0")
            await h.send(FOREMAN, "Текст")
            await h.send(FOREMAN, "Без фото")
            old_save = h.button(FOREMAN, "save")
            old_obj = h.button(FOREMAN, "obj", "1")
            await h.send(FOREMAN, "🧱 Заявка")             # бросил запись, начал заявку
            await h.press_raw(FOREMAN, old_obj)            # кнопка объекта из прошлого сценария
            await h.press_raw(FOREMAN, old_save)           # «Сохранить» из прошлого сценария
            await h.press_raw(FOREMAN, "obj:zzz:5")        # мусор
            return h
        h = run(go())
        self.assertEqual(h.db.sheets["Записи"], [])
        texts = h.session.texts_to(FOREMAN)
        self.assertGreaterEqual(sum("из прошлого сообщения" in t or "устарела" in t for t in texts), 3)
        self.assertNotIn("Объект: Стена", texts)

    def test_stale_due_button_rejected(self):
        async def go():
            h = Harness()
            await h.send(FOREMAN, "🧱 Заявка")
            await h.press(FOREMAN, "obj", "0")
            await h.press(FOREMAN, "rt", "0")
            await h.send(FOREMAN, "Песок 10 т")
            nonce = h.button(FOREMAN, "due", (datetime.now().date() + timedelta(days=1)).isoformat()).split(":")[1]
            await h.press_raw(FOREMAN, f"due:{nonce}:2020-01-01")
            return h
        h = run(go())
        self.assertTrue(any("кнопка устарела" in t.lower() for t in h.session.texts_to(FOREMAN)))

    def test_retry_after_lost_write_does_not_duplicate(self):
        async def go():
            h = Harness()
            real_append = h.db.append
            calls = {"n": 0}

            async def flaky(sheet, row):           # запись дошла, но ответ потерялся
                calls["n"] += 1
                await real_append(sheet, row)
                if calls["n"] == 1:
                    raise TimeoutError("lost")
            h.db.append = flaky
            await h.send(DIRECTOR, "📝 Запись по объекту")
            await h.press(DIRECTOR, "obj", "0")
            await h.press(DIRECTOR, "nt", "4")
            await h.send(DIRECTOR, "Проверка связи")
            await h.send(DIRECTOR, "Без фото")
            data = h.button(DIRECTOR, "save")
            await h.press_raw(DIRECTOR, data)
            await h.press_raw(DIRECTOR, data)
            return h
        h = run(go())
        self.assertEqual(len(h.db.sheets["Записи"]), 1)
        self.assertTrue(any("Запись сохранена" in t for t in h.session.texts_to(DIRECTOR)))

    def test_bad_machinery_reasked(self):
        async def go():
            h = Harness()
            await h.send(FOREMAN, "📋 Отчёт за день")
            await h.press(FOREMAN, "obj", "0")
            await h.send(FOREMAN, "4")
            await h.send(FOREMAN, "Пропустить")
            await h.send(FOREMAN, "экскаватор 8ч кран 4ч")
            return h
        h = run(go())
        self.assertTrue(any("Пишите через запятую" in t for t in h.session.texts_to(FOREMAN)))

    def test_supply_cannot_send_reports_but_gets_notified(self):
        async def go():
            h = Harness()
            await h.send(SUPPLY, "📋 Отчёт за день")
            return h
        h = run(go())
        self.assertTrue(any("Отправлять данные по объектам могут" in t for t in h.session.texts_to(SUPPLY)))

    def test_unknown_role_and_storage_down(self):
        async def go():
            h = Harness()
            h.db.sheets["Пользователи"][0]["Роль"] = "прорабище"
            await h.send(FOREMAN, "/start")
            from greenline_bot.storage import StorageUnavailable

            async def down():
                raise StorageUnavailable("нет сети")
            h.db.list_users = down
            await h.send(DIRECTOR, "/start")
            return h
        h = run(go())
        self.assertTrue(any("не распознана" in t for t in h.session.texts_to(FOREMAN)))
        self.assertTrue(any("недоступна" in t for t in h.session.texts_to(DIRECTOR)))


if __name__ == "__main__":
    unittest.main()

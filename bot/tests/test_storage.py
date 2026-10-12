import asyncio
import unittest

from greenline_bot.storage import SCHEMA, MemoryStorage, StorageUnavailable, align


def run(coro):
    return asyncio.run(coro)


class Memory(unittest.TestCase):
    def test_pending_user_once_and_inactive(self):
        db = MemoryStorage()
        run(db.add_pending_user(10, "Иван", "ivan", "12.10.2026"))
        run(db.add_pending_user(10, "Иван", "ivan", "12.10.2026"))
        users = run(db.list_users())
        self.assertEqual(len(users), 1)
        self.assertFalse(users[0].active)

    def test_demo_user_is_active_director(self):
        db = MemoryStorage(demo_mode=True)
        run(db.add_pending_user(10, "Иван", "ivan", "12.10.2026"))
        u = run(db.list_users())[0]
        self.assertTrue(u.active and u.role == "директор" and u.sees_all)
        self.assertEqual(len(run(db.list_objects())), 2)

    def test_unknown_column_rejected(self):
        with self.assertRaises(KeyError):
            run(MemoryStorage().append("Заявки", {"Нет такой": 1}))

    def test_open_requests_and_delivery(self):
        db = MemoryStorage()
        for rid, obj, status, delivery in [("ЗК-1", "О-01", "Новая", "нет"),
                                           ("ЗК-2", "О-01", "Закрыто", "получено"),
                                           ("ЗК-3", "О-02", "Новая", "нет"),
                                           ("ЗК-4", "О-01", "Оплачено", "частично")]:
            run(db.append("Заявки", {"ID": rid, "Объект ID": obj, "Статус": status,
                                     "Поставка": delivery}))
        self.assertEqual([r["ID"] for r in run(db.open_requests("О-01"))], ["ЗК-1", "ЗК-4"])
        self.assertTrue(run(db.set_request_delivery("ЗК-4", "получено")))
        self.assertEqual([r["ID"] for r in run(db.open_requests("О-01"))], ["ЗК-1"])
        self.assertFalse(run(db.set_request_delivery("нет", "получено")))

    def test_text_stored_as_is(self):
        # В таблицу пишем RAW: формулы не выполняются, апостроф не нужен.
        db = MemoryStorage()
        run(db.append("Ошибки", {"ID": "Б-1", "Текст": "+7 913 000"}))
        self.assertEqual(db.sheets["Ошибки"][0]["Текст"], "+7 913 000")
        self.assertTrue(run(db.exists("Ошибки", "Б-1")))
        self.assertFalse(run(db.exists("Ошибки", "Б-2")))

    def test_align_follows_real_headers(self):
        # Помощник вставил свою колонку посередине — данные не съезжают.
        headers = ["ID", "Мой комментарий", "Дата", "Текст", "Статус", "Автор ID", "Автор"]
        self.assertEqual(align(headers, {"ID": "Б-1", "Текст": "т", "Дата": "д"}),
                         ["Б-1", "", "д", "т", "", "", ""])
        with self.assertRaises(StorageUnavailable):
            align(["ID", "Дата"], {"ID": "Б-1", "Текст": "т"})

    def test_schema_has_bitrix_id_on_data_sheets(self):
        for name in ["Отчёты", "Заявки", "Приёмка", "Записи", "Объекты"]:
            self.assertIn("Битрикс ID", SCHEMA[name])


if __name__ == "__main__":
    unittest.main()

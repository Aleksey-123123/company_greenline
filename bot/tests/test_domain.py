import unittest
from datetime import date, datetime

from greenline_bot.domain import (InputError, SiteObject, User, archive_link, new_id,
                                  objects_for, parse_due_date, parse_machinery,
                                  parse_object_list, parse_people, users_to_notify)

OBJS = [SiteObject("О-01", "Мост"), SiteObject("О-02", "Стена"),
        SiteObject("О-03", "Закрыт", active=False)]


class Access(unittest.TestCase):
    def test_foreman_sees_only_his_objects(self):
        u = User(1, "Иван", "прораб", True, {"О-02", "О-03"})
        self.assertEqual([o.id for o in objects_for(u, OBJS)], ["О-02"])  # О-03 закрыт

    def test_foreman_with_several_objects(self):
        u = User(1, "Иван", "прораб", True, {"О-01", "О-02"})
        self.assertEqual(len(objects_for(u, OBJS)), 2)

    def test_director_sees_all_active(self):
        u = User(2, "Дир", "директор", True)
        self.assertEqual([o.id for o in objects_for(u, OBJS)], ["О-01", "О-02"])

    def test_all_word(self):
        objs, flag = parse_object_list("Все")
        self.assertTrue(flag)
        self.assertEqual(objs, set())
        u = User(3, "РП", "рп", True, objs, flag)
        self.assertEqual(len(objects_for(u, OBJS)), 2)

    def test_object_list_parsing(self):
        self.assertEqual(parse_object_list("О-01, О-02;О-03\n")[0], {"О-01", "О-02", "О-03"})
        self.assertEqual(parse_object_list("")[0], set())

    def test_notify_filters_role_object_and_author(self):
        users = [User(1, "Прораб", "прораб", True, {"О-01"}),
                 User(2, "Снаб", "снабжение", True),
                 User(3, "РП другой", "рп", True, {"О-02"}),
                 User(4, "РП", "рп", True, {"О-01"}),
                 User(5, "Дир неакт", "директор", False)]
        got = users_to_notify(users, {"снабжение", "рп", "директор"}, "О-01", exclude_id=4)
        self.assertEqual([u.telegram_id for u in got], [2])


class Money(unittest.TestCase):
    def test_amount_real_chat_lines(self):
        from greenline_bot.domain import parse_amount
        a = parse_amount("Заборная сетка 75 метров 9900 нал")
        self.assertEqual((a.what, a.amount, a.method, a.unpaid), ("Заборная сетка 75 метров", 9900, "наличные", False))
        a = parse_amount("12000 люди нал. ( не оплатил)")
        self.assertEqual((a.what, a.amount, a.method, a.unpaid), ("люди", 12000, "наличные", True))
        a = parse_amount("Цемент 2 034,50 б/н")
        self.assertEqual((a.amount, a.method), (2034.5, "безнал"))
        for bad in ["сетка", "Сетка 75 м", "9900"]:
            with self.assertRaises(InputError, msg=bad):
                parse_amount(bad)

    def test_money_and_quantity(self):
        from greenline_bot.domain import parse_money, parse_quantity
        self.assertEqual([parse_money(x) for x in ["50000", "50 000", "50к", "50000₽"]], [50000.0] * 4)
        self.assertEqual(parse_quantity("82,6 т"), (82.6, "т"))
        self.assertEqual(parse_quantity("40 мешков"), (40.0, "мешок"))
        self.assertEqual(parse_quantity("12 м3"), (12.0, "м³"))
        self.assertEqual(parse_quantity("35,95т."), (35.95, "т"))
        with self.assertRaises(InputError):
            parse_quantity("много")

    def test_balance_counts_only_paid_cash(self):
        from greenline_bot.domain import cash_balance
        issued = [{"Кому ID": 1, "Сумма": 50000}, {"Кому ID": 2, "Сумма": 9999}]
        exp = [{"Автор ID": 1, "Сумма": 9900, "Способ оплаты": "наличные", "Оплачено": "да"},
               {"Автор ID": 1, "Сумма": 12000, "Способ оплаты": "наличные", "Оплачено": "нет"},
               {"Автор ID": 1, "Сумма": 5000, "Способ оплаты": "безнал", "Оплачено": "да"}]
        self.assertEqual(cash_balance(1, issued, exp), 40100)

    def test_sheet_url(self):
        from greenline_bot.config import sheet_id_from
        url = "https://docs.google.com/spreadsheets/d/1AbC-d_9/edit#gid=0"
        self.assertEqual(sheet_id_from(url), "1AbC-d_9")
        self.assertEqual(sheet_id_from("1AbC-d_9"), "1AbC-d_9")


class Parsing(unittest.TestCase):
    def test_people(self):
        self.assertEqual(parse_people("6"), 6)
        self.assertEqual(parse_people(" 12 чел "), 12)
        for bad in ["шесть", "", "-1", "1000"]:
            with self.assertRaises(InputError):
                parse_people(bad)

    def test_machinery_simple(self):
        r = parse_machinery("экскаватор 8, кран 4,5 ч")
        self.assertEqual([(m.name, m.qty, m.unit) for m in r.items],
                         [("экскаватор", 8.0, "ч"), ("кран", 4.5, "ч")])
        self.assertEqual((r.hours, r.trips, r.cost), (12.5, 0, 0))
        self.assertEqual(parse_machinery("Нет").items, [])
        self.assertEqual(parse_machinery("экскаватор 8 ч и кран 4 ч").hours, 12.0)
        self.assertEqual(parse_machinery("КамАЗ-самосвал: 6 часов; манипулятор 3").hours, 9.0)

    def test_machinery_real_chat_formats(self):
        # Форматы из рабочего чата прораба (notes/0024), числа изменены.
        cases = {
            "Экскаватор погрузчик 9,5 часов по 3000 = 28500": (9.5, 0, 28500),
            "4 часа гидромолот по 4000\n5 часов ковш по 3000": (9, 0, 31000),
            "Каток 10 часов 30000": (10, 0, 30000),
            "5 хово по 9000 безнал": (0, 5, 45000),
            "Хово 1 рейс 12000": (0, 1, 12000),
            "Экскаватор погрузчик 3000х8=24000": (8, 0, 24000),
            "КамАЗ 10000 нал": (0, 1, 10000),
            # «= итог» в конце двух строк: 4×4000 + 6×3000 = 34000
            "Экскаватор погрузчик 4 часа гидромолот по 4000\n6 часов ковш по 3000 = 34000": (10, 0, 34000),
        }
        for text, (h, t, c) in cases.items():
            r = parse_machinery(text)
            self.assertEqual((r.hours, r.trips, r.cost), (h, t, c), msg=text)

    def test_hint_example_parses(self):
        # Пример, который бот показывает прорабу, обязан разбираться.
        import re
        from greenline_bot.domain import MACHINERY_HINT
        example = re.search(r"«(.+?)»", MACHINERY_HINT).group(1)
        r = parse_machinery(example)
        self.assertEqual((r.hours, r.trips), (19.0, 5.0))

    def test_machinery_errors(self):
        # Всё, что разбирается не целиком или не сходится, — переспрос, а не тихая ошибка.
        for bad in ["экскаватор весь день", "кран 30", "экскаватор 8ч кран 4ч",
                    "2 экскаватора по 8 ч", "экскаватор и кран 8", "8 часов",
                    "10,5 по 3500= 36750", "Каток 10 часов по 3500 = 30000",
                    "Хово с 29.9 по 4.10 424000 р.", "экскаватор 31500"]:
            with self.assertRaises(InputError, msg=bad):
                parse_machinery(bad)

    def test_due_date(self):
        today = date(2026, 10, 12)
        self.assertEqual(parse_due_date("15.10", today), date(2026, 10, 15))
        self.assertEqual(parse_due_date("10.01", today), date(2027, 1, 10))  # без года → будущее
        self.assertEqual(parse_due_date("01.11.26", today), date(2026, 11, 1))
        for bad in ["завтра", "31.02", "01.10.2026", "01.01.2030"]:
            with self.assertRaises(InputError):
                parse_due_date(bad, today)

    def test_misc(self):
        rid = new_id("ЗК", datetime(2026, 10, 12, 14, 32))
        self.assertTrue(rid.startswith("ЗК-261012-1432-") and len(rid) == len("ЗК-261012-1432-") + 6)
        self.assertEqual(archive_link(-1001234567, 42), "https://t.me/c/1234567/42")


if __name__ == "__main__":
    unittest.main()

"""Проверки tools/mask.py.  Запуск:  python3 -m unittest tools/test_mask.py -v

Каждая проверка — случай из разбора супервизора (`notes/0028`, раздел 5):
что утекало или портилось и теперь не должно.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mask import mask, unmask  # noqa: E402

DICT = [
    ("Сибмост-Инжиниринг", "Заказчик-02"),
    ("Сибмост", "Заказчик-01"),
    ("Иванов", "Человек-01"),
    ("Пётр", "Человек-02"),
    ("Заказ", "Слово-01"),  # нарочно: основа, которая есть внутри кода «Заказчик»
]

LETTER = (
    "Письмо в ООО «Сибмост» (ИНН 5406123456, КПП 540601001) от Сибмост-Инжиниринга: "
    "прорабу Иванову Петру позвонить +7 (913) 123-45-67 или 913-765-43-21, "
    "почта ivanov@sibmost.ru. р/с 40702810123450000001, БИК 045004641, "
    "карта 2202 2000 1234 5678. Паспорт 50 12 345678, СНИЛС 123-456-789 01. "
    "Сумма договора 1234567890 руб., объём 1 250,5 м3."
)


class MaskTest(unittest.TestCase):
    def setUp(self):
        self.out, self.new, self.counts, self.warn = mask(LETTER, DICT)

    def test_nothing_secret_left(self):
        for secret in ["Сибмост", "Иванов", "Петр", "5406123456", "540601001",
                       "123-45-67", "765-43-21", "ivanov@", "40702810123450000001",
                       "045004641", "2202 2000", "50 12 345678", "123-456-789"]:
            self.assertNotIn(secret, self.out, secret)

    def test_amounts_kept(self):
        # сумма из 10 цифр без слова «ИНН» — это сумма, а не ИНН
        self.assertIn("1234567890 руб.", self.out)
        self.assertIn("1 250,5 м3", self.out)

    def test_short_stem_does_not_break_codes(self):
        self.assertIn("Заказчик-01", self.out)
        self.assertIn("Заказчик-02", self.out)
        self.assertNotIn("Слово-01чик", self.out)

    def test_word_boundary(self):
        out, *_ = mask("Мостовой переход у Сибмоста", [("Мост", "Объект-01")])
        self.assertIn("Объект-01", out)  # «Мостовой» — с начала слова, ловится
        self.assertIn("Сибмоста", out)  # а внутри слова — нет

    def test_initials_warned(self):
        _, _, _, warn = mask("Согласовано: Петров П.П.", [])
        self.assertTrue(any("Петров" in w for w in warn))

    def test_roundtrip_tolerates_case_and_case_endings(self):
        pairs = DICT + self.new
        answer = "Заказчика‑01 должен; заказчик-02 тоже. ИНН-01 сверить."
        back = unmask(answer, pairs)
        self.assertEqual(back, "Сибмост должен; Сибмост-Инжиниринг тоже. 5406123456 сверить.")

    def test_unmask_does_not_confuse_01_and_010(self):
        pairs = [("Альфа", "Заказчик-01"), ("Бета", "Заказчик-010")]
        self.assertEqual(unmask("Заказчик-010 и Заказчик-01", pairs), "Бета и Альфа")


if __name__ == "__main__":
    unittest.main()

"""Обезличивание текста перед отправкой во внешний ИИ — и обратно.

Зачем: в Claude уходят не названия заказчиков, ФИО и реквизиты, а коды
(«Заказчик-01», «Человек-03»). Таблица соответствия лежит только у нас
(`data/маска-словарь.csv`, в git не попадает — см. `.gitignore`).
Словарь — ключ ко всему обезличиванию: хранить там же, где сами данные,
с теми же правами доступа.

Почему не нейросеть: правило словаря либо сработало, либо нет, и это видно
в отчёте. Нейросеть ошибается молча. Нейросеть — следующий шаг, только если
словарь не справится (`notes/0028`, шаг 3).

Запуск (только стандартная библиотека Python 3):

    python3 tools/mask.py mask   входной.txt  > обезличенный.txt
    python3 tools/mask.py unmask ответ-claude.txt > ответ-с-именами.txt

Вход — текст (.txt, .csv, .md). Word/Excel/PDF сначала сохранить как текст
или CSV («Сохранить как»).

Словарь — CSV `основа,код`. Пишите ОСНОВУ слова, не короче 4 букв:
«Сибмост» поймает «Сибмоста», «Сибмосту». ФИО — отдельными строками:
фамилия (`Иванов`) и, если нужно, имя (`Пётр`/`Петр` ловятся одинаково).
Основа срабатывает только с начала слова: «Мост» не заденет «Сибмост»,
но заденет «Мостовой» — поэтому лучше длинные основы.

Сами находит и заменяет кодом: ИНН, КПП, ОГРН, БИК (только рядом с этими
словами — иначе любая сумма из 10 цифр выглядела бы как ИНН), расчётный
счёт (20 цифр), номер карты, телефон, e-mail, паспорт, СНИЛС. Новые
находки дописываются в конец словаря, чтобы `unmask` вернул их обратно.

После замены печатает ПРЕДУПРЕЖДЕНИЯ: что осталось похожим на ФИО
с инициалами и на длинный номер. Их — глазами.

Суммы и объёмы не трогаются: они нужны для расчёта.
`unmask` возвращает основу из словаря, без падежа («Сибмост», а не
«Сибмоста»).
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path

DEFAULT_DICT = Path(__file__).resolve().parent.parent / "data" / "маска-словарь.csv"
MIN_STEM = 4
DASH = "[-‐‑‒–—]"

# (вид, regex, номер группы со значением). Реквизиты по ключевому слову —
# чтобы сумма «1234567890 руб.» не стала ИНН.
PATTERNS: list[tuple[str, str, int]] = [
    ("ИНН", r"(?i)(\bИНН\s*[:№]?\s*)(\d{12}|\d{10})\b", 2),
    ("КПП", r"(?i)(\bКПП\s*[:№]?\s*)(\d{9})\b", 2),
    ("ОГРН", r"(?i)(\bОГРН(?:ИП)?\s*[:№]?\s*)(\d{15}|\d{13})\b", 2),
    ("БИК", r"(?i)(\bБИК\s*[:№]?\s*)(\d{9})\b", 2),
    ("Счёт", r"()\b(\d{20})\b", 2),
    ("Карта", r"()\b(\d{4}[ -]?\d{4}[ -]?\d{4}[ -]?\d{4})\b", 2),
    ("СНИЛС", r"()\b(\d{3}-\d{3}-\d{3}[ -]\d{2})\b", 2),
    ("Паспорт", r"()\b(\d{2}\s?\d{2}\s№?\s?\d{6})\b", 2),
    ("Телефон", r"()((?:\+7|\b[78])[\s(-]*9\d{2}[\s)-]*\d{3}[\s-]*\d{2}[\s-]*\d{2}\b"
                r"|\b9\d{2}[\s-]\d{3}[\s-]?\d{2}[\s-]?\d{2}\b)", 2),
    ("Почта", r"()\b([\w.+-]+@[\w-]+\.[\w.-]+)\b", 2),
]

WARN_INITIALS = re.compile(r"\b[А-ЯЁ][а-яё]{2,}\s+[А-ЯЁ]\.\s?[А-ЯЁ]\.|\b[А-ЯЁ]\.\s?[А-ЯЁ]\.\s?[А-ЯЁ][а-яё]{2,}")
WARN_LONGNUM = re.compile(r"\b\d{9,}\b")


def load_dict(path: Path) -> list[tuple[str, str]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as f:
        rows = [r for r in csv.reader(f) if r and r[0].strip() and not r[0].startswith("#")]
    if rows and rows[0][0].strip().lower() == "основа":
        rows = rows[1:]
    pairs = [(r[0].strip(), r[1].strip()) for r in rows]
    short = [s for s, _ in pairs if len(s) < MIN_STEM]
    if short:
        sys.exit(f"ошибка: основы короче {MIN_STEM} знаков заденут лишнее: {short}")
    codes = [c for _, c in pairs]
    dup = sorted({c for c in codes if codes.count(c) > 1})
    if dup:
        sys.exit(f"ошибка: в словаре повторяются коды {dup} — unmask будет неоднозначным")
    return pairs


def append_dict(path: Path, new: list[tuple[str, str]]) -> None:
    """Дописывает новые находки в конец словаря, не трогая то, что там уже есть."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fresh = not path.exists() or path.stat().st_size == 0
    with path.open("a", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        if fresh:
            w.writerow(["основа", "код"])
        w.writerows(new)


def _stem_rx(stem: str) -> str:
    return "".join("[её]" if ch in "её" else re.escape(ch) for ch in stem)


def mask(text: str, pairs: list[tuple[str, str]]) -> tuple[str, list[tuple[str, str]], dict[str, int], list[str]]:
    counts: dict[str, int] = {}
    known = {stem: code for stem, code in pairs}
    new: list[tuple[str, str]] = []

    # 1. реквизиты по шаблонам (раньше словаря: e-mail «ivanov@…» целиком)
    for kind, pattern, grp in PATTERNS:
        def repl(m: re.Match[str], kind: str = kind, grp: int = grp) -> str:
            value = m.group(grp)
            if value not in known:
                n = sum(1 for c in known.values() if c.startswith(kind + "-")) + 1
                known[value] = f"{kind}-{n:02d}"
                new.append((value, known[value]))
            code = known[value]
            counts[code] = counts.get(code, 0) + 1
            return m.group(0)[: m.start(grp) - m.start(0)] + code

        text = re.sub(pattern, repl, text)

    # 2. словарь — ОДНИМ проходом, чтобы основа не сработала внутри уже
    # вставленного кода; длинные основы раньше коротких
    if pairs:
        ordered = sorted(pairs, key=lambda p: -len(p[0]))
        lookup = {s.lower().replace("ё", "е"): c for s, c in ordered}
        rx = re.compile(r"\b(" + "|".join(_stem_rx(s) for s, _ in ordered) + r")[а-яёa-z]*", re.IGNORECASE)

        def repl_dict(m: re.Match[str]) -> str:
            code = lookup[m.group(1).lower().replace("ё", "е")]
            counts[code] = counts.get(code, 0) + 1
            return code

        text = rx.sub(repl_dict, text)

    warnings = [f"похоже на ФИО: «{w}»" for w in WARN_INITIALS.findall(text)]
    warnings += [f"длинный номер не замаскирован: {w}" for w in WARN_LONGNUM.findall(text)]
    return text, new, counts, warnings


def unmask(text: str, pairs: list[tuple[str, str]]) -> str:
    """Код → основа. Терпит регистр, падеж («Заказчика-01») и разные дефисы."""
    by_code: dict[tuple[str, str], str] = {}
    for stem, code in pairs:
        m = re.fullmatch(r"(.+?)-(\d+)", code)
        if m:
            by_code[(m.group(1).lower(), m.group(2))] = stem
    if not by_code:
        return text
    words = sorted({w for w, _ in by_code}, key=len, reverse=True)
    rx = re.compile(r"(" + "|".join(re.escape(w) for w in words) + r")[а-яё]*" + DASH + r"(\d+)(?!\d)", re.IGNORECASE)

    def repl(m: re.Match[str]) -> str:
        return by_code.get((m.group(1).lower(), m.group(2)), m.group(0))

    return rx.sub(repl, text)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["mask", "unmask"])
    ap.add_argument("file", type=Path, help="входной файл (txt, csv, md)")
    ap.add_argument("--dict", type=Path, default=DEFAULT_DICT, help="словарь (по умолчанию data/маска-словарь.csv)")
    args = ap.parse_args()

    text = args.file.read_text(encoding="utf-8-sig")
    pairs = load_dict(args.dict)

    if args.command == "unmask":
        sys.stdout.write(unmask(text, pairs))
        return

    result, new, counts, warnings = mask(text, pairs)
    if new:
        append_dict(args.dict, new)
    sys.stdout.write(result)

    err = sys.stderr
    print(f"\n--- заменено: {sum(counts.values())} вхождений, кодов: {len(counts)}; новых в словаре: {len(new)}", file=err)
    for code, n in sorted(counts.items()):
        print(f"    {code}: {n}", file=err)
    if warnings:
        print(f"--- ПРЕДУПРЕЖДЕНИЯ ({len(warnings)}) — проверить и дописать в словарь:", file=err)
        for w in warnings:
            print(f"    {w}", file=err)
    print("--- Прочитайте результат глазами перед отправкой: словарь ловит только то, что в нём есть.", file=err)


if __name__ == "__main__":
    main()

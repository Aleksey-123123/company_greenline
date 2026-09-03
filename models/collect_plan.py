#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Очередь взыскания: в каком порядке собирать деньги, которые уже заработаны.

Ранжирует источники по ожидаемым деньгам в неделю:
    приоритет = сумма × вероятность / недели до денег

Такой порядок максимизирует деньги на счёте в ближайший месяц, а не «сумму
на бумаге». Крупный, но безнадёжный долг проигрывает трём мелким и быстрым.

Запуск:
    python3 models/collect_plan.py --file data/receivables.example.csv
"""

import argparse
import csv
import sys


class DataError(Exception):
    pass


def load(path: str) -> list:
    rows = []
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if not r.get("id") or r["id"].strip().startswith("#"):
                continue
            try:
                amount = float(r["amount"])
                prob = float(r["probability"])
                weeks = float(r["weeks"])
            except (KeyError, ValueError) as e:
                raise DataError(f"строка {r.get('id', '?')}: {e}")
            if not 0.0 <= prob <= 1.0:
                raise DataError(f"[{r['id']}] probability должна быть от 0 до 1, "
                                f"получено {prob}")
            if weeks <= 0:
                raise DataError(f"[{r['id']}] weeks должно быть > 0")
            rows.append({
                "id": r["id"].strip(),
                "source": r["source"].strip(),
                "object": r["object"].strip(),
                "amount": amount,
                "prob": prob,
                "weeks": weeks,
                "action": r.get("action", "").strip(),
                "owner": r.get("owner", "").strip(),
                "expected": amount * prob,
                "per_week": amount * prob / weeks,
            })
    if not rows:
        raise DataError(f"в файле {path} нет строк")
    return rows


def money(x: float) -> str:
    return f"{x:>13,.0f}".replace(",", " ")


def report(rows: list) -> None:
    rows = sorted(rows, key=lambda r: r["per_week"], reverse=True)

    print("\nОЧЕРЕДЬ ВЗЫСКАНИЯ — сверху то, что даёт деньги быстрее всего\n")
    head = (f"{'#':<4}{'Источник':<26}{'Объект':<20}{'сумма ₽':>14}"
            f"{'вер.':>6}{'нед':>5}{'ожид. ₽':>14}{'₽/нед':>13}  Ответственный")
    print(head)
    print("-" * len(head))
    for i, r in enumerate(rows, 1):
        print(f"{i:<4}{r['source'][:25]:<26}{r['object'][:19]:<20}"
              f"{money(r['amount'])}{r['prob']:>6.0%}{r['weeks']:>5.0f}"
              f"{money(r['expected'])}{money(r['per_week'])}  {r['owner']}")

    total = sum(r["amount"] for r in rows)
    exp = sum(r["expected"] for r in rows)
    near = sum(r["expected"] for r in rows if r["weeks"] <= 4)

    print("\nИТОГО")
    print(f"  Заявлено к взысканию:              {money(total)} ₽")
    print(f"  Ожидаемо с учётом вероятности:     {money(exp)} ₽")
    print(f"  Из них в горизонте 4 недель:       {money(near)} ₽")

    print("\nПЕРВЫЕ ТРИ ДЕЙСТВИЯ НА ЭТУ НЕДЕЛЮ")
    for r in rows[:3]:
        print(f"  • [{r['id']}] {r['source']} — {r['object']}")
        print(f"    {r['action'] or 'действие не задано — заполните колонку action'}"
              f"  → {r['owner'] or 'ответственный не назначен'}")
    print()


def main() -> int:
    ap = argparse.ArgumentParser(description="Приоритет взыскания заработанных денег")
    ap.add_argument("--file", default="data/receivables.example.csv")
    args = ap.parse_args()
    try:
        report(load(args.file))
    except DataError as e:
        print(f"ОШИБКА ДАННЫХ: {e}", file=sys.stderr)
        return 1
    except FileNotFoundError:
        print(f"Файл не найден: {args.file}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

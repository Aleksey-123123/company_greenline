#!/usr/bin/env python3
"""Сводка воронки потенциальных проектов.

Считает кодом то, что иначе считают «в уме»: сумму заявленных объёмов по
стадиям, сколько позиций без суммы, и какие позиции упираются в стоп-факторы
чек-листа (модель A, постоплата, нет следующего шага / ответственного / срока).

    python3 models/pipeline.py --file data/pipeline.example.csv

Вероятности намеренно не используются: их нет в данных, а выдуманная
вероятность превращает воронку в самоуспокоение.
"""
import argparse
import csv
import sys
from collections import OrderedDict

REQUIRED = ["id", "name", "channel", "stage", "amount", "model", "postpay",
            "next_step", "owner", "due"]


def rub(x):
    return f"{x:,.0f}".replace(",", " ")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default="data/pipeline.example.csv")
    args = ap.parse_args()

    with open(args.file, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        sys.exit("пустой файл")
    missing = [c for c in REQUIRED if c not in rows[0]]
    if missing:
        sys.exit(f"нет колонок: {missing}")

    by_stage = OrderedDict()
    total, no_amount = 0, []
    flags, closed = [], []
    for r in rows:
        if r.get("stage", "").strip() == "отказ":
            closed.append((r["id"], r["name"], r["next_step"]))
            continue
        amt = int(r["amount"]) if r["amount"].strip() else None
        if amt is None and r.get("qty", "").strip() and r.get("unit_price", "").strip():
            amt = int(r["qty"]) * int(r["unit_price"])   # объём × цена за единицу
            r["amount"] = str(amt)
        st = by_stage.setdefault(r["stage"], {"n": 0, "sum": 0, "no_sum": 0})
        st["n"] += 1
        if amt is None:
            st["no_sum"] += 1
            no_amount.append(r["id"])
        else:
            st["sum"] += amt
            total += amt
        f = []
        if r["model"].strip() == "A":
            f.append("модель A (свои материалы)")
        if r["postpay"].strip() == "да":
            f.append("постоплата")
        if r["model"].strip() == "?":
            f.append("модель не определена")
        for c in ("next_step", "owner", "due"):
            if not r[c].strip():
                f.append(f"нет поля {c}")
        if f:
            flags.append((r["id"], r["name"], f))

    print(f"Позиций в воронке: {len(rows) - len(closed)} (отказов: {len(closed)})")
    print(f"Сумма заявленных объёмов: {rub(total)} ₽ "
          f"(без суммы: {len(no_amount)} — {', '.join(no_amount) or 'нет'})\n")
    print(f"{'стадия':<18}{'шт':>4}{'сумма, ₽':>16}{'без суммы':>11}")
    for k, v in by_stage.items():
        print(f"{k:<18}{v['n']:>4}{rub(v['sum']):>16}{v['no_sum']:>11}")
    if total:
        print("\nДоля крупнейших позиций в заявленной сумме:")
        closed_ids = {c[0] for c in closed}
        sized = sorted(((int(r["amount"]), r["id"], r["name"]) for r in rows
                        if r["amount"].strip() and r["id"] not in closed_ids), reverse=True)
        vat = {r["id"]: r.get("vat", "?") for r in rows}
        for amt, i, n in sized:
            print(f"  {i} {n[:45]:<45} {rub(amt):>13} ₽  {amt / total:6.1%}  {vat[i]}")
        print("  (НДС у позиций разный — суммы сравнимы только приблизительно)")
    if closed:
        print("\nОтказы:")
        for i, n, why in closed:
            print(f"  {i} {n[:45]:<45} {why}")
    print("\nФлаги (до решения — через templates/чек-лист-нового-проекта.md):")
    for i, n, f in flags:
        print(f"  {i} {n[:45]:<45} {'; '.join(f)}")


if __name__ == "__main__":
    main()

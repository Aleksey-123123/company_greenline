#!/usr/bin/env python3
"""Сверка цели 2027 с фактом 2025–2026 и размер проектов против оборота.

Исходные цифры — ответы директора 2026-11-01 (notes/0027). Запуск:
    python3 models/goals_check.py
"""
FACT = {2025: (55e6, 3e6), 2026: (90e6, 5e6)}   # выручка, прибыль, руб
GOAL_MARGIN = 0.20                                # «маржа от 20 %»
GOAL_PROFIT = 2e6                                 # «прибыль от 2 млн чистыми»
STOP_SHARE = 0.30                                 # стоп-фактор 3 чек-листа
PROJECTS = {"L-01 Петербург": 140e6, "L-07 Спорткомплекс": 120e6,
            "L-06 ГБШ": 10e6, "L-02 Маслянино": 9e6}


def m(x):
    return f"{x / 1e6:,.1f}".replace(",", " ") + " млн"


print("1. Рентабельность по прибыли (прибыль / выручка):")
for y, (rev, pr) in FACT.items():
    print(f"   {y}: {m(pr)} / {m(rev)} = {pr / rev:.1%}")
g = FACT[2026][0] / FACT[2025][0] - 1
print(f"   рост выручки 2026 к 2025: {g:.1%}")

print("\n2. Цель «2 млн чистыми» — два прочтения:")
print(f"   в год:   {m(GOAL_PROFIT)} < прибыль 2026 {m(FACT[2026][1])} — цель уже"
      f" перевыполнена, прочтение маловероятно")
yr = GOAL_PROFIT * 12
print(f"   в месяц: {m(yr)} в год = {yr / FACT[2026][1]:.1f}× прибыли 2026")
print(f"   при марже {GOAL_MARGIN:.0%} нужна выручка {m(yr / GOAL_MARGIN)} в год"
      f" ({yr / GOAL_MARGIN / FACT[2026][0]:.2f}× выручки 2026)")
print(f"   при текущей марже {FACT[2026][1] / FACT[2026][0]:.1%} нужна выручка"
      f" {m(yr / (FACT[2026][1] / FACT[2026][0]))}")

base = FACT[2026][0]
print(f"\n3. Проекты против выручки 2026 ({m(base)}); порог стоп-фактора 3 ="
      f" {STOP_SHARE:.0%} = {m(base * STOP_SHARE)}:")
for k, v in PROJECTS.items():
    flag = "СТОП-ФАКТОР 3" if v > base * STOP_SHARE else "ок"
    print(f"   {k:<22}{m(v):>11}  {v / base:6.1%}  {flag}")
both = PROJECTS["L-01 Петербург"] + PROJECTS["L-07 Спорткомплекс"]
print(f"   Петербург + спорткомплекс: {m(both)} = {both / base:.1%} выручки 2026")

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Купить коробку (Gectaro) или сделать своё: сколько стоит каждый путь за N месяцев.

Своё = часы разработки × ставка + поддержка в месяц × ставка + хостинг
       (+ подписка Битрикс24, если офисный контур на нём).
Коробка = абонентская плата × месяцы (+ разовое внедрение, если есть).

Время сотрудников на ввод данных одинаково в обоих путях и не учитывается.

Запуск:
    python3 models/system_tco.py --modules data/system_build.example.csv
    python3 models/system_tco.py --modules data/system_build.example.csv \
        --box-monthly 56000 --rates 2000 3000 --support-hours 16
"""

import argparse
import csv
import sys


class DataError(Exception):
    pass


def load_modules(path: str) -> list:
    rows = []
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if not r.get("id") or r["id"].strip().startswith("#"):
                continue
            try:
                hours = float(r["hours"])
            except (KeyError, ValueError) as e:
                raise DataError(f"строка {r.get('id', '?')}: {e}")
            if hours <= 0:
                raise DataError(f"[{r['id']}] hours должно быть > 0")
            rows.append({"id": r["id"], "module": r["module"], "hours": hours,
                         "in_box": r.get("in_gectaro", "").strip()})
    if not rows:
        raise DataError("в файле нет ни одного модуля")
    return rows


def money(x: float) -> str:
    return f"{x:,.0f}".replace(",", " ")


def own_cost(months: int, build_h: float, rate: float, support_h: float,
             hosting: float, office_sub: float) -> float:
    return build_h * rate + months * (support_h * rate + hosting + office_sub)


def box_cost(months: int, monthly: float, setup: float) -> float:
    return setup + months * monthly


def breakeven_month(build_h, rate, support_h, hosting, office_sub,
                    monthly, setup, horizon=120):
    """Первый месяц, с которого своё дешевле коробки нарастающим итогом."""
    for m in range(1, horizon + 1):
        if own_cost(m, build_h, rate, support_h, hosting, office_sub) <= \
                box_cost(m, monthly, setup):
            return m
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--modules", required=True)
    ap.add_argument("--box-monthly", type=float, default=35000,
                    help="абонентская плата коробки, ₽/мес (по умолчанию 35 000 — 8 польз.)")
    ap.add_argument("--box-setup", type=float, default=0,
                    help="разовое внедрение коробки, ₽")
    ap.add_argument("--rates", type=float, nargs="+", default=[1500, 2500, 4000],
                    help="сценарии ставки разработчика, ₽/ч")
    ap.add_argument("--support-hours", type=float, default=12,
                    help="поддержка своей системы, ч/мес")
    ap.add_argument("--hosting", type=float, default=2000, help="сервер, ₽/мес")
    ap.add_argument("--office-sub", type=float, default=0,
                    help="подписка офисного контура (Битрикс24), ₽/мес")
    ap.add_argument("--horizons", type=int, nargs="+", default=[12, 24, 36])
    a = ap.parse_args()

    try:
        mods = load_modules(a.modules)
    except (DataError, OSError) as e:
        print(f"ОШИБКА: {e}", file=sys.stderr)
        return 1

    build_h = sum(m["hours"] for m in mods)
    print(f"Модули своей системы ({a.modules}):")
    for m in mods:
        print(f"  {m['id']}  {m['hours']:>5.0f} ч  [в коробке: {m['in_box'] or '?'}]  {m['module']}")
    print(f"  ИТОГО разработка: {build_h:.0f} ч; поддержка {a.support_hours:.0f} ч/мес; "
          f"хостинг {money(a.hosting)} ₽/мес; офисная подписка {money(a.office_sub)} ₽/мес\n")

    head = f"{'':<22}" + "".join(f"{f'{h} мес':>14}" for h in a.horizons) + f"{'окупаемость':>16}"
    print(head)
    print(f"{'Коробка':<22}" + "".join(
        f"{money(box_cost(h, a.box_monthly, a.box_setup)):>14}" for h in a.horizons))
    for r in a.rates:
        be = breakeven_month(build_h, r, a.support_hours, a.hosting, a.office_sub,
                             a.box_monthly, a.box_setup)
        be_s = f"{be} мес" if be else "нет за 10 лет"
        print(f"{f'Своё @ {money(r)} ₽/ч':<22}" + "".join(
            f"{money(own_cost(h, build_h, r, a.support_hours, a.hosting, a.office_sub)):>14}"
            for h in a.horizons) + f"{be_s:>16}")

    print(f"\nЕжемесячно после запуска (поддержка + хостинг + подписка) против коробки "
          f"{money(a.box_monthly)} ₽/мес:")
    for r in a.rates:
        run = a.support_hours * r + a.hosting + a.office_sub
        print(f"  @ {money(r)} ₽/ч: {money(run)} ₽/мес ({run / a.box_monthly:.0%} от коробки)")

    year_box = box_cost(12, a.box_monthly, a.box_setup)
    print("\nГод коробки в часах разработчика:")
    for r in a.rates:
        print(f"  @ {money(r)} ₽/ч: {year_box / r:,.0f} ч".replace(",", " "))
    return 0


if __name__ == "__main__":
    sys.exit(main())

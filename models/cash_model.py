#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Триаж проектов по деньгам, а не по марже.

Отвечает на один вопрос по каждому проекту: он ДАЁТ свободные деньги
или СВЯЗЫВАЕТ их — и сколько зарабатывает на каждый связанный рубль в год.

Запуск:
    python3 models/cash_model.py --projects data/projects.example.csv
    python3 models/cash_model.py --projects data/projects.csv --hurdle 0.6

Ключевой принцип обвязки: считаем кодом. Скрипт либо сходится, либо падает
с ошибкой на противоречивых данных — молча неверный результат он не выдаёт.
"""

import argparse
import csv
import sys
from dataclasses import dataclass

# Порог отдачи на связанный рубль (годовых, доля). Ниже него связывать
# собственные деньги в проекте невыгодно: дешевле не строить за свой счёт.
# Ориентир — стоимость денег для компании (кредит/факторинг) плюс премия за риск.
DEFAULT_HURDLE = 0.60


class DataError(Exception):
    """Исходные данные противоречивы — считать нельзя."""


@dataclass
class Project:
    pid: str
    name: str
    ptype: str            # A = материалы наши, B = материалы заказчика
    contract_sum: float   # сумма контракта, руб
    billed: float         # закрыто КС-2/КС-3 нарастающим итогом, руб
    received: float       # фактически получено денег, руб
    cost_materials: float # остаток затрат на материалы (наши деньги), руб
    cost_works: float     # остаток затрат на работы/технику/бригады, руб
    months_left: float    # месяцев до окончания работ
    retention_pct: float  # гарантийное удержание, %

    # --- производные величины ---

    @property
    def receivable(self) -> float:
        """Нам должны: закрыто, но не оплачено."""
        return self.billed - self.received

    @property
    def revenue_left(self) -> float:
        """Выручка, которую ещё предстоит заработать."""
        return self.contract_sum - self.billed

    @property
    def cost_left(self) -> float:
        return self.cost_materials + self.cost_works

    @property
    def margin_left(self) -> float:
        """Маржа на остатке проекта, руб."""
        return self.revenue_left - self.cost_left

    @property
    def margin_pct(self) -> float:
        if self.revenue_left <= 0:
            return 0.0
        return self.margin_left / self.revenue_left

    @property
    def retention_locked(self) -> float:
        """Деньги, которые удержат по гарантии со всей суммы контракта."""
        return self.contract_sum * self.retention_pct / 100.0

    @property
    def cash_tied(self) -> float:
        """
        Сколько СВОИХ денег проект связывает до конца:
        остаток затрат + уже зависшая дебиторка + гарантийное удержание.
        Это и есть цена участия в проекте.
        """
        return self.cost_left + self.receivable + self.retention_locked

    @property
    def cash_velocity(self) -> float:
        """
        Отдача на связанный рубль в годовом выражении.
        Главная метрика при дефиците оборотных средств: не «сколько процентов
        маржи», а «сколько зарабатывает каждый замороженный рубль за год».
        """
        if self.cash_tied <= 0:
            return float("inf")   # проект денег не требует
        months = max(self.months_left, 0.5)
        return (self.margin_left / self.cash_tied) * (12.0 / months)

    def verdict(self, hurdle: float) -> str:
        if self.margin_left <= 0:
            return "ВЫХОД / ПЕРЕСОГЛАСОВАТЬ ЦЕНУ"
        if self.cash_tied <= 0:
            return "ДЕРЖАТЬ — капитал не связан"
        if self.ptype.upper() == "B":
            return "ДЕРЖАТЬ — приоритет"
        if self.cash_velocity >= hurdle:
            return "ДЕРЖАТЬ, но выбить аванс"
        return "ПЕРЕВЕСТИ В МОДЕЛЬ B / СУБПОДРЯД"


def validate(p: Project) -> None:
    """Проверки, которые ловят опечатки в исходнике до того, как они станут выводом."""
    if p.contract_sum <= 0:
        raise DataError(f"[{p.pid}] сумма контракта должна быть > 0")
    if p.billed > p.contract_sum + 1e-6:
        raise DataError(
            f"[{p.pid}] закрыто КС ({p.billed:,.0f}) больше суммы контракта "
            f"({p.contract_sum:,.0f}) — проверьте допсоглашения")
    if p.received > p.billed + 1e-6:
        raise DataError(
            f"[{p.pid}] получено ({p.received:,.0f}) больше, чем закрыто "
            f"({p.billed:,.0f}) — это аванс? вынесите его отдельной строкой")
    if p.ptype.upper() not in ("A", "B"):
        raise DataError(f"[{p.pid}] type должен быть A или B, получено '{p.ptype}'")
    if p.months_left < 0:
        raise DataError(f"[{p.pid}] months_left не может быть отрицательным")
    if p.ptype.upper() == "B" and p.cost_materials > 0:
        raise DataError(
            f"[{p.pid}] модель B (материалы заказчика), но указаны затраты на "
            f"материалы {p.cost_materials:,.0f} — либо это модель A, либо ошибка")


def load(path: str) -> list:
    projects = []
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if not row.get("id") or row["id"].strip().startswith("#"):
                continue
            try:
                p = Project(
                    pid=row["id"].strip(),
                    name=row["name"].strip(),
                    ptype=row["type"].strip(),
                    contract_sum=float(row["contract_sum"]),
                    billed=float(row["billed"]),
                    received=float(row["received"]),
                    cost_materials=float(row["cost_materials"]),
                    cost_works=float(row["cost_works"]),
                    months_left=float(row["months_left"]),
                    retention_pct=float(row["retention_pct"]),
                )
            except (KeyError, ValueError) as e:
                raise DataError(f"строка {row.get('id', '?')}: {e}")
            validate(p)
            projects.append(p)
    if not projects:
        raise DataError(f"в файле {path} нет ни одного проекта")
    return projects


def money(x: float) -> str:
    return f"{x:>14,.0f}".replace(",", " ")


def report(projects: list, hurdle: float) -> None:
    projects = sorted(projects, key=lambda p: p.cash_velocity, reverse=True)

    print("\nТРИАЖ ПРОЕКТОВ — отдача на связанный рубль")
    print(f"Порог (hurdle): {hurdle:.0%} годовых на собственный вложенный рубль\n")

    head = (f"{'ID':<6}{'Проект':<26}{'М':<3}"
            f"{'связано ₽':>15}{'маржа ₽':>15}{'маржа %':>9}"
            f"{'отдача/год':>12}  Решение")
    print(head)
    print("-" * len(head))

    for p in projects:
        vel = "капитал 0" if p.cash_velocity == float("inf") else f"{p.cash_velocity:>11.0%}"
        print(f"{p.pid:<6}{p.name[:25]:<26}{p.ptype.upper():<3}"
              f"{money(p.cash_tied)}{money(p.margin_left)}{p.margin_pct:>8.0%} "
              f"{vel}  {p.verdict(hurdle)}")

    # ---- сводка ----
    tied_all = sum(p.cash_tied for p in projects)
    tied_a = sum(p.cash_tied for p in projects if p.ptype.upper() == "A")
    materials_a = sum(p.cost_materials for p in projects if p.ptype.upper() == "A")
    receivable = sum(p.receivable for p in projects)
    retention = sum(p.retention_locked for p in projects)
    margin_all = sum(p.margin_left for p in projects)

    print("\nСВОДКА ПО ДЕНЬГАМ")
    print(f"  Связано собственных денег всего:      {money(tied_all)} ₽")
    print(f"    в т.ч. в проектах модели A:         {money(tied_a)} ₽")
    print(f"    из них материалы (остаток закупки): {money(materials_a)} ₽")
    print(f"  Дебиторка (закрыто, не оплачено):     {money(receivable)} ₽")
    print(f"  Гарантийные удержания:                {money(retention)} ₽")
    print(f"  Маржа на остатке портфеля:            {money(margin_all)} ₽")

    print("\nЭФФЕКТ ПЕРЕВОДА ВСЕХ ПРОЕКТОВ A НА ДАВАЛЬЧЕСКИЕ МАТЕРИАЛЫ")
    print(f"  Высвобождается сразу:                 {money(materials_a)} ₽")
    if tied_all > 0:
        print(f"  Это {materials_a / tied_all:.0%} всех связанных денег компании.")

    print("\nБЫСТРЫЕ ДЕНЬГИ БЕЗ НОВЫХ ПРОДАЖ (взыскание + возврат удержаний)")
    print(f"  Потенциал:                            {money(receivable + retention)} ₽")
    print("  Срок: 2–8 недель. Источник — notes/0002, горизонт 0–30 дней.\n")

    outs = [p for p in projects if p.margin_left <= 0]
    if outs:
        print("⚠  Проекты с нулевой или отрицательной маржой на остатке — "
              "каждый день работы там увеличивает убыток:")
        for p in outs:
            print(f"   [{p.pid}] {p.name}: маржа {money(p.margin_left)} ₽")
        print()


def main() -> int:
    ap = argparse.ArgumentParser(description="Триаж строительных проектов по денежному потоку")
    ap.add_argument("--projects", default="data/projects.example.csv",
                    help="CSV с проектами (по умолчанию — демо-данные)")
    ap.add_argument("--hurdle", type=float, default=DEFAULT_HURDLE,
                    help=f"порог отдачи на связанный рубль, доля годовых (по умолч. {DEFAULT_HURDLE})")
    args = ap.parse_args()
    try:
        report(load(args.projects), args.hurdle)
    except DataError as e:
        print(f"ОШИБКА ДАННЫХ: {e}", file=sys.stderr)
        return 1
    except FileNotFoundError:
        print(f"Файл не найден: {args.projects}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

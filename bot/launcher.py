"""Запуск бота «для людей»: зовётся из start.bat / start.sh и prepare-table.bat / .sh.

    python launcher.py          — поставить библиотеки, создать .env, запустить бота
    python launcher.py table    — подготовить Google Таблицу (setup_sheet)

Русские сообщения живут здесь, а не в .bat: cmd.exe плохо читает кириллицу
в UTF-8 и может молча закрыть окно.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENV, EXAMPLE = HERE / ".env", HERE / ".env.example"


def say(text: str = "") -> None:
    print(text, flush=True)


def install() -> bool:
    say("Проверяю библиотеки (первый раз — 1–3 минуты)...")
    r = subprocess.run([sys.executable, "-m", "pip", "install", "-q", "--disable-pip-version-check",
                        "-r", str(HERE / "requirements.txt")])
    if r.returncode != 0:
        say("\nНе удалось поставить библиотеки. Проверьте интернет и запустите ещё раз.")
        return False
    return True


def open_editor(path: Path) -> None:
    if os.name == "nt":
        subprocess.run(["notepad.exe", str(path)])        # ждёт, пока Блокнот закроют
    else:
        say(f"Откройте и заполните файл: {path}")


def token_filled() -> bool:
    for line in ENV.read_text(encoding="utf-8-sig").splitlines():
        if line.strip().startswith("BOT_TOKEN="):
            return bool(line.split("=", 1)[1].strip())
    return False


def ensure_env() -> bool:
    (HERE / "secrets").mkdir(exist_ok=True)
    if not ENV.exists():
        shutil.copy(EXAMPLE, ENV)
        say("\nСоздан файл настроек .env (он в этой же папке, рядом с start.bat).")
        say("Сейчас он откроется в Блокноте: вставьте токен бота сразу после BOT_TOKEN= ,")
        say("сохраните (Ctrl+S) и закройте Блокнот.")
        open_editor(ENV)
    if not token_filled():
        say("\nВ файле .env не заполнен BOT_TOKEN. Открываю его ещё раз.")
        open_editor(ENV)
        if not token_filled():
            say(f"\nТокен так и не вписан. Файл настроек: {ENV}")
            return False
    return True


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "bot"
    if not install() or not ensure_env():
        return 1
    if mode == "table":
        return subprocess.run([sys.executable, "-m", "greenline_bot.setup_sheet"], cwd=HERE).returncode
    say("\nБот запускается. Пока это окно открыто — бот работает. Закрыть окно = остановить бота.\n")
    code = subprocess.run([sys.executable, "-m", "greenline_bot"], cwd=HERE).returncode
    say("\nБот остановлен.")
    return code


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        say("\nОстановлено.")

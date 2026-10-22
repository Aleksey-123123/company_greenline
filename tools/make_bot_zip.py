"""Собрать greenline-bot.zip — только папка bot/ для скачивания на компьютер.

    python3 tools/make_bot_zip.py

Без тестов, кэша, .env и ключей. Пересобирать после изменений в bot/.
"""

import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {"tests", "__pycache__", ".venv", "secrets"}
SKIP_FILES = {".env"}


def main() -> None:
    out = ROOT / "greenline-bot.zip"
    files = sorted(p for p in (ROOT / "bot").rglob("*")
                   if p.is_file() and not SKIP_DIRS & set(p.relative_to(ROOT).parts)
                   and p.name not in SKIP_FILES and p.suffix != ".pyc")
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for p in files:
            name = p.relative_to(ROOT).as_posix()
            if p.suffix == ".bat":       # cmd.exe надёжно читает только CRLF
                data = p.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
                z.writestr(name, data)
            elif p.suffix == ".sh":      # исполняемый на Mac/Linux
                info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
                info.external_attr = 0o755 << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                z.writestr(info, p.read_bytes())
            else:
                z.write(p, name)
        z.writestr("bot/secrets/положите-сюда-service-account.json.txt",
                   "В эту папку положить ключ Google под именем service-account.json\n")
    print(f"{out.name}: {len(files) + 1} файлов, {out.stat().st_size} байт")


if __name__ == "__main__":
    main()

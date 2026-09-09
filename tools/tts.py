#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Сценарий → аудиофайл. Озвучка заметок для прослушивания с телефона.

Вход:  audio/scripts/*.md — звуковые сценарии (проза, числа словами).
Выход: audio/out/*.mp3 + audio/out/все-заметки.m4a (одним файлом с главами).

Движок: RHVoice — русский синтезатор, работает офлайн, голоса ставятся
пакетом с PyPI. Выбран не по качеству, а по доступности: нейросетевые
голоса (piper, silero) тянут модели с huggingface и github, а сетевая
политика окружения их не пропускает. Проверить: tools/setup-tts.sh

Использование:
    tools/setup-tts.sh                      # один раз: поставить движок
    python3 tools/tts.py --list-voices
    python3 tools/tts.py                    # озвучить все сценарии
    python3 tools/tts.py --only 0002 --voice anna
    python3 tools/tts.py --rate 1.1 --no-join
"""

import argparse
import os
import re
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "audio" / "scripts"
OUT = ROOT / "audio" / "out"

# Голос по умолчанию. Список доступных — ключ --list-voices.
DEFAULT_VOICE = "anna"

# --- нормализация текста под синтез -----------------------------------------
# Сценарии пишутся уже вслух (числа словами), это подстраховка на остатки
# разметки и символы, которые синтезатор читает не так, как надо.
REPLACEMENTS = [
    (r"₽", " рублей"),
    (r"№", " номер "),
    (r"%", " процентов"),
    (r"&", " и "),
    (r"[«»“”„]", ""),
    (r"[–—]", ", "),          # тире → пауза, иначе читается как «минус»
    (r"\.\.\.", "."),
    (r"\bт\.\s*е\.", "то есть"),
    (r"\bт\.\s*д\.", "так далее"),
    (r"\bт\.\s*п\.", "тому подобное"),
    (r"\bи\.\s*о\.", "исполняющий обязанности"),
    (r"\bмлн\b", "миллионов"),
    (r"\bтыс\b", "тысяч"),
    (r"\bруб\b", "рублей"),
    (r"\bг\.\s*Новосибирск", "Новосибирск"),
    (r"\bм²", "квадратных метров"),
    (r"\bкм\b", "километров"),
]


def strip_front_matter(text: str) -> str:
    """Убрать YAML-шапку сценария — она для человека, не для озвучки."""
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            return text[end + 4:]
    return text


def normalize(text: str) -> str:
    text = strip_front_matter(text)
    # остатки разметки: заголовки, выделения, ссылки, код, таблицы
    text = re.sub(r"```.*?```", "", text, flags=re.S)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"^\s*\|.*$", "", text, flags=re.M)        # таблицы не читаем
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.M)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)     # ссылки → текст
    text = re.sub(r"[*_]{1,3}([^*_]+)[*_]{1,3}", r"\1", text)
    text = re.sub(r"^\s*[-•]\s+", "", text, flags=re.M)
    text = re.sub(r"\[\?\]|\[!\]", "", text)                 # служебные пометки
    for pat, rep in REPLACEMENTS:
        text = re.sub(pat, rep, text)
    # пути к файлам на слух — мусор
    text = re.sub(r"\b[\w/.-]+\.(md|py|csv|txt)\b", "", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def chunks(text: str, limit: int = 1200):
    """Резать по абзацам: синтез длинного текста одним куском менее устойчив."""
    buf = ""
    for para in [p.strip() for p in text.split("\n\n") if p.strip()]:
        if len(buf) + len(para) > limit and buf:
            yield buf
            buf = para
        else:
            buf = f"{buf}\n\n{para}" if buf else para
    if buf:
        yield buf


# --- синтез ------------------------------------------------------------------

# Русские голоса RHVoice. Остальные из пакета — других языков.
RU_VOICES = ("anna", "aleksandr", "elena", "irina", "anatol", "natalia")


def get_tts():
    try:
        from rhvoice_wrapper import TTS
    except ImportError:
        sys.exit("RHVoice не установлен. Запустите tools/setup-tts.sh")
    return TTS(threads=1)   # без quiet=: обёртка такого ключа не знает


def pick_voice(tts, requested: str) -> str:
    """Проверить, что голос есть. Иначе — первый доступный русский."""
    available = {str(v).lower() for v in tts.voices}
    if requested.lower() in available:
        return requested.lower()
    for v in RU_VOICES:
        if v in available:
            print(f"голос '{requested}' недоступен, беру '{v}'")
            return v
    sys.exit(f"русских голосов нет, доступны: {sorted(available)}")


def synth_parts(tts, text: str, tmpdir: Path, voice: str, rate: float) -> list:
    """
    Синтез по кускам, каждый — в свой wav силами самого RHVoice.
    Так не нужно знать частоту дискретизации движка: заголовок пишет он.
    """
    sets = {"absolute_rate": rate} if rate else None
    paths = []
    for i, chunk in enumerate(chunks(text)):
        dest = tmpdir / f"part{i:03d}.wav"
        try:
            tts.to_file(filename=str(dest), text=chunk,
                        voice=voice, format_="wav", sets=sets)
        except TypeError:
            # старая версия обёртки не принимает sets
            tts.to_file(filename=str(dest), text=chunk,
                        voice=voice, format_="wav")
        if not dest.exists() or dest.stat().st_size < 1000:
            sys.exit(f"кусок {i} не синтезировался: {dest}")
        paths.append(dest)
    return paths


def get_espeak(voice: str, rate: float):
    """
    Движок создаётся ОДИН раз на процесс. Повторный espeak_Initialize
    оставляет зарегистрированным колбэк первого экземпляра, и у второго
    буфер молча остаётся пустым — синтез «проходит», а звука нет.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from espeak_backend import EspeakTTS
    # rate -1..1 → слов в минуту, 170 это обычный темп
    return EspeakTTS(voice=voice, wpm=int(170 * (1 + 0.4 * rate)))


def synth_parts_espeak(tts, text: str, tmpdir: Path) -> list:
    """Запасной синтез: espeak-ng. Хуже на слух, но не требует сборки."""
    paths = []
    for i, chunk in enumerate(chunks(text)):
        dest = tmpdir / f"part{i:03d}.wav"
        tts.to_wav(chunk, dest)
        paths.append(dest)
    return paths


def wav_duration(paths: list) -> float:
    total = 0.0
    for p in paths:
        with wave.open(str(p)) as w:
            total += w.getnframes() / w.getframerate()
    return total


# --- кодирование -------------------------------------------------------------

def ffmpeg_bin() -> str:
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        from shutil import which
        exe = which("ffmpeg")
        if not exe:
            sys.exit("ffmpeg не найден. Запустите tools/setup-tts.sh")
        return exe


def concat_list(paths: list, dest: Path) -> Path:
    dest.write_text("".join(f"file '{p}'\n" for p in paths), encoding="utf-8")
    return dest


def to_mp3(ff: str, parts: list, mp3: Path, title: str, track: int) -> None:
    lst = concat_list(parts, mp3.parent / f"_{mp3.stem}.txt")
    subprocess.run(
        [ff, "-y", "-loglevel", "error",
         "-f", "concat", "-safe", "0", "-i", str(lst),
         "-codec:a", "libmp3lame", "-b:a", "64k", "-ac", "1",
         "-metadata", f"title={title}",
         "-metadata", "album=Заметки company_greenline",
         "-metadata", "artist=RHVoice",
         "-metadata", "genre=Speech",
         "-metadata", f"track={track}",
         str(mp3)],
        check=True)
    lst.unlink(missing_ok=True)


def join_with_chapters(ff: str, items: list, dest: Path) -> None:
    """Склеить главы в один файл — удобно слушать в машине без выбора трека."""
    lst = concat_list([mp3 for mp3, _, _ in items], OUT / "_join.txt")

    meta = [";FFMETADATA1", "album=Заметки company_greenline", "artist=RHVoice"]
    start = 0
    for _, title, dur in items:
        end = start + int(dur * 1000)
        meta += ["[CHAPTER]", "TIMEBASE=1/1000",
                 f"START={start}", f"END={end}", f"title={title}"]
        start = end
    meta_f = OUT / "_chapters.txt"
    meta_f.write_text("\n".join(meta) + "\n", encoding="utf-8")

    subprocess.run(
        [ff, "-y", "-loglevel", "error",
         "-f", "concat", "-safe", "0", "-i", str(lst),
         "-i", str(meta_f), "-map_metadata", "1",
         "-codec:a", "aac", "-b:a", "64k", "-ac", "1",
         str(dest)],
        check=True)
    lst.unlink(missing_ok=True)
    meta_f.unlink(missing_ok=True)


def title_of(path: Path, text: str) -> str:
    """Заголовок главы — первая содержательная строка сценария."""
    for line in text.split("\n"):
        line = line.strip()
        if line and not line.startswith("---"):
            return line.rstrip(".")
    return path.stem


def main() -> int:
    ap = argparse.ArgumentParser(description="Озвучка звуковых сценариев")
    ap.add_argument("--voice", default=DEFAULT_VOICE)
    ap.add_argument("--rate", type=float, default=0.0,
                    help="темп речи: -1 медленно … 0 обычно … 1 быстро")
    ap.add_argument("--only", help="озвучить только сценарии с этой подстрокой")
    ap.add_argument("--no-join", action="store_true",
                    help="не собирать общий файл с главами")
    ap.add_argument("--engine", choices=("rhvoice", "espeak"), default="rhvoice",
                    help="rhvoice — лучше на слух; espeak — работает всегда")
    ap.add_argument("--list-voices", action="store_true")
    args = ap.parse_args()

    if args.list_voices:
        print(", ".join(sorted(str(v) for v in get_tts().voices)))
        return 0

    files = sorted(SCRIPTS.glob("*.md"))
    if args.only:
        files = [f for f in files if args.only in f.name]
    if not files:
        sys.exit(f"нет сценариев в {SCRIPTS}")

    OUT.mkdir(parents=True, exist_ok=True)
    ff = ffmpeg_bin()
    if args.engine == "rhvoice":
        tts = get_tts()
        voice = pick_voice(tts, args.voice)
    else:
        voice = "ru" if args.voice == DEFAULT_VOICE else args.voice
        tts = get_espeak(voice, args.rate)
    items, total = [], 0.0

    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        for n, path in enumerate(files, 1):
            text = normalize(path.read_text(encoding="utf-8"))
            title = title_of(path, text)
            mp3 = OUT / f"{path.stem}.mp3"
            print(f"[{n}/{len(files)}] {path.name} — "
                  f"{len(text.split())} слов … ", end="", flush=True)

            part_dir = tmpdir / path.stem
            part_dir.mkdir()
            if args.engine == "rhvoice":
                parts = synth_parts(tts, text, part_dir, voice, args.rate)
            else:
                parts = synth_parts_espeak(tts, text, part_dir)
            dur = wav_duration(parts)
            to_mp3(ff, parts, mp3, title, n)

            total += dur
            items.append((mp3, title, dur))
            print(f"{dur/60:.1f} мин, {mp3.stat().st_size/1e6:.1f} МБ")

    if not args.no_join and len(items) > 1:
        dest = OUT / "все-заметки.m4a"
        join_with_chapters(ff, items, dest)
        print(f"\nодним файлом с главами: {dest.name} "
              f"({dest.stat().st_size/1e6:.1f} МБ)")

    print(f"итого {total/60:.1f} мин, движок {args.engine}, "
          f"голос {voice}, темп {args.rate}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

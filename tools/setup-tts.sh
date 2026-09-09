#!/usr/bin/env sh
# Установка офлайн-синтеза речи для tools/tts.py
#
# Основной движок — RHVoice: русский синтезатор, голоса ставятся пакетом с
# PyPI. Второй, espeak-ng, ставится всегда как страховка: он приезжает
# готовой библиотекой и не требует компиляции, но звучит роботом.
#
# Почему не нейросетевые голоса: piper и silero тянут модели с huggingface и
# github. Если сеть их пропускает — берите piper, он звучит живее RHVoice;
# команда в конце файла.
#
# Грабли сборки RHVoice, из-за которых она падает (все три учтены ниже):
#   1. компилируется C++ — нужны g++, make и scons;
#   2. scons должен быть виден в PATH САМОЙ СБОРКИ: она вызывает его как
#      внешнюю команду, поэтому установки внутрь venv недостаточно;
#   3. pip изолирует сборку, и тогда scons не может импортировать себя —
#      нужен --no-build-isolation (а значит, setuptools и wheel заранее);
#   4. сборка клонирует исходники RHVoice с GitHub — нужен доступ туда;
#   5. компиляция идёт минут десять. Это нормально, не зависание.
set -e

VENV="${VENV:-.venv-tts}"

for tool in g++ make; do
    command -v "$tool" >/dev/null || {
        echo "нет $tool — поставьте компилятор (apt install build-essential)"
        exit 1
    }
done

python3 -m venv "$VENV"
"$VENV/bin/pip" install -q --upgrade pip setuptools wheel
"$VENV/bin/pip" install -q scons
"$VENV/bin/pip" install -q espeakng-loader          # запасной движок
"$VENV/bin/pip" install -q imageio-ffmpeg           # ffmpeg бинарником
"$VENV/bin/pip" install -q rhvoice-wrapper rhvoice-wrapper-data

echo "Собираю RHVoice, это займёт около десяти минут…"
VENV_ABS=$(cd "$VENV" && pwd)
PATH="$VENV_ABS/bin:$PATH" "$VENV/bin/pip" install --no-build-isolation \
    rhvoice-wrapper-bin || {
    echo
    echo "RHVoice не собрался. Это не тупик: работайте вторым движком —"
    echo "  $VENV/bin/python tools/tts.py --engine espeak"
    exit 0
}

echo
echo "Готово. Дальше:"
echo "  $VENV/bin/python tools/tts.py --list-voices"
echo "  $VENV/bin/python tools/tts.py"
echo
echo "Если доступен huggingface, голос будет живее с piper:"
echo "  $VENV/bin/pip install piper-tts"
echo "  $VENV/bin/python -m piper.download_voices ru_RU-dmitri-medium"

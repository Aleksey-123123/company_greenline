#!/usr/bin/env sh
# Установка офлайн-синтеза речи для tools/tts.py
#
# Почему RHVoice, а не нейросетевые голоса: piper и silero тянут модели с
# huggingface и github, а сетевая политика окружения их не пропускает
# (CONNECT → 403). RHVoice ставит голоса пакетом с PyPI, поэтому работает.
# Если сеть открыта — piper даст звук заметно живее, см. комментарий ниже.
#
# Сборка rhvoice-wrapper-bin компилирует C++: нужны g++, make и scons.
set -e

VENV="${VENV:-.venv-tts}"

python3 -m venv "$VENV"
"$VENV/bin/pip" install -q --upgrade pip
"$VENV/bin/pip" install -q scons                 # нужен для сборки движка
"$VENV/bin/pip" install -q rhvoice-wrapper rhvoice-wrapper-data
"$VENV/bin/pip" install    rhvoice-wrapper-bin   # компиляция, несколько минут
"$VENV/bin/pip" install -q imageio-ffmpeg        # ffmpeg бинарником из пакета

echo
echo "Готово. Дальше:"
echo "  $VENV/bin/python tools/tts.py --list-voices"
echo "  $VENV/bin/python tools/tts.py"
echo
echo "Если доступен huggingface, лучше взять piper (голос живее):"
echo "  pip install piper-tts && python -m piper.download_voices ru_RU-dmitri-medium"

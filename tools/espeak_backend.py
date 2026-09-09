#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Запасной движок синтеза: espeak-ng через ctypes.

Нужен потому, что основной движок (RHVoice) требует компиляции C++, а она
может не пройти в чужом окружении. espeak-ng приезжает готовой библиотекой
в пакете espeakng-loader с PyPI и работает всегда.

Качество заметно хуже RHVoice: espeak-ng — формантный синтез, звучит
роботом. Использовать, только если RHVoice не собрался.

    from espeak_backend import EspeakTTS
    tts = EspeakTTS(voice="ru", wpm=170)
    tts.to_wav("Привет", Path("out.wav"))
"""

import ctypes
import wave
from pathlib import Path

AUDIO_OUTPUT_RETRIEVAL = 1
CHARS_UTF8 = 1
RATE_PARAM = 1          # espeakRATE
POSITION_CHARACTER = 1

CALLBACK = ctypes.CFUNCTYPE(
    ctypes.c_int, ctypes.POINTER(ctypes.c_short), ctypes.c_int, ctypes.c_void_p)


class EspeakTTS:
    def __init__(self, voice: str = "ru", wpm: int = 170):
        try:
            import espeakng_loader
        except ImportError as e:
            raise RuntimeError(
                "нет espeakng-loader: pip install espeakng-loader") from e

        self._lib = ctypes.CDLL(str(espeakng_loader.get_library_path()))
        self._lib.espeak_Initialize.restype = ctypes.c_int
        rate = self._lib.espeak_Initialize(
            AUDIO_OUTPUT_RETRIEVAL, 0,
            str(espeakng_loader.get_data_path()).encode(), 0)
        if rate <= 0:
            raise RuntimeError("espeak_Initialize не удался")
        self.sample_rate = rate

        if self._lib.espeak_SetVoiceByName(voice.encode()) != 0:
            raise RuntimeError(f"голос '{voice}' недоступен в espeak-ng")
        self._lib.espeak_SetParameter(RATE_PARAM, int(wpm), 0)

        self._buf = bytearray()
        # ссылку на callback держим в self, иначе её соберёт сборщик мусора
        self._cb = CALLBACK(self._on_samples)
        self._lib.espeak_SetSynthCallback(self._cb)

    def _on_samples(self, wav, numsamples, events):
        if wav and numsamples > 0:
            self._buf += ctypes.string_at(wav, numsamples * 2)
        return 0

    def synth(self, text: str) -> bytes:
        """Синтез в сырой PCM 16 бит моно."""
        self._buf = bytearray()
        data = text.encode("utf-8")
        err = self._lib.espeak_Synth(
            data, len(data) + 1, 0, POSITION_CHARACTER, 0,
            CHARS_UTF8, None, None)
        if err != 0:
            raise RuntimeError(f"espeak_Synth вернул ошибку {err}")
        self._lib.espeak_Synchronize()
        if not self._buf:
            raise RuntimeError("espeak не выдал ни одного сэмпла")
        return bytes(self._buf)

    def to_wav(self, text: str, dest: Path) -> float:
        """Записать wav. Возвращает длительность в секундах."""
        pcm = self.synth(text)
        with wave.open(str(dest), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(self.sample_rate)
            w.writeframes(pcm)
        return len(pcm) / 2 / self.sample_rate


if __name__ == "__main__":
    import sys
    t = EspeakTTS()
    d = t.to_wav(sys.argv[1] if len(sys.argv) > 1 else
                 "Проверка синтеза русской речи. Пять миллионов рублей.",
                 Path("/tmp/espeak-test.wav"))
    print(f"частота {t.sample_rate} Гц, длительность {d:.2f} с")

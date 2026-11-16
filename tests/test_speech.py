import math
import struct
import sys
import wave

import pytest

from byte_agent.speech import SpeechEngine, clean_for_speech, wav_envelope
from fakes import FAKE_TTS


def write_wav(path, seconds=1.0, rate=8000, width=2, loud_until=0.5):
    frames = bytearray()
    for i in range(int(rate * seconds)):
        amp = 0.6 if i < rate * loud_until else 0.0
        value = amp * math.sin(i * 0.25)
        if width == 2:
            frames += struct.pack("<h", int(value * 32767))
        elif width == 1:
            frames += bytes([int(128 + value * 127)])
        else:
            frames += struct.pack("<i", int(value * 2147483647))
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(width)
        w.setframerate(rate)
        w.writeframes(bytes(frames))


@pytest.mark.parametrize("width", [1, 2, 4])
def test_envelope_follows_the_audio(tmp_path, width):
    path = tmp_path / "a.wav"
    write_wav(path, width=width)
    duration, mouth = wav_envelope(path, fps=30)
    assert abs(duration - 1.0) < 0.01 and 29 <= len(mouth) <= 31
    assert all(0.0 <= v <= 1.0 for v in mouth)
    assert max(mouth[2:13]) > 0.8 and max(mouth[-8:]) < 0.1        # boca aberta no trecho com som, fechada no silêncio


def test_silent_file_keeps_mouth_closed(tmp_path):
    path = tmp_path / "s.wav"
    write_wav(path, loud_until=0.0)
    assert set(wav_envelope(path)[1]) == {0.0}


def test_clean_for_speech():
    assert clean_for_speech("Olá **chat**! Veja https://x.com/a 😀 @fulano `código`") == "Olá chat ! Veja fulano código"


@pytest.fixture()
def engine_factory(tmp_path):
    script = tmp_path / "fake_tts.py"
    script.write_text(FAKE_TTS, encoding="utf-8")

    def make(extra=""):
        return SpeechEngine(f"{sys.executable} {script} {{out}}{extra}", player_command="none", work_dir=tmp_path / "tts")
    return make


def test_speak_animates_mouth_in_real_time_and_cleans_up(engine_factory, tmp_path):
    engine = engine_factory()
    assert engine.available
    values = []
    import time
    t0 = time.monotonic()
    result = engine.speak("Oi, chat!", on_mouth=values.append)
    elapsed = time.monotonic() - t0
    assert result["spoken"] and abs(result["duration_s"] - 0.4) < 0.02
    assert 0.35 <= elapsed < 1.5                                   # bloqueia pelo tempo da fala
    assert max(values) > 0.8 and values[-1] == 0.0                 # termina de boca fechada
    assert list((tmp_path / "tts").glob("*.wav")) == []            # o WAV temporário é apagado


def test_chat_text_cannot_inject_shell_commands(engine_factory, tmp_path):
    marker = tmp_path / "invadido"
    engine = engine_factory()
    result = engine.speak(f"oi; touch {marker} $(touch {marker}) `touch {marker}`")
    assert result["spoken"] and not marker.exists()


def test_avatar_failure_does_not_stop_the_voice(engine_factory):
    def broken(_):
        raise RuntimeError("VTube Studio fechado")
    assert engine_factory().speak("teste", on_mouth=broken)["spoken"] is True


def test_tts_failure_is_reported(tmp_path):
    engine = SpeechEngine(f"{sys.executable} -c 'import sys; sys.exit(3)' {{out}}", player_command="none", work_dir=tmp_path)
    result = engine.speak("teste")
    assert result["spoken"] is False and "código 3" in result["error"]
    with pytest.raises(ValueError):
        SpeechEngine("piper --model x.onnx")                      # sem {out}
    assert SpeechEngine("").available is False

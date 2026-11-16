"""Voz da Byte: TTS por comando externo (ex.: Piper) + reprodução + sincronia labial a partir do áudio.

O texto NUNCA passa por um shell: o comando é separado com shlex e o texto vai por stdin (ou por um
argumento próprio, se o modelo de comando tiver {text}). Texto de chat não consegue injetar comandos.

    BYTE_TTS_COMMAND="piper --model /caminho/pt_BR-faber-medium.onnx --output_file {out}"
    BYTE_AUDIO_PLAYER="pw-play {file}"        # opcional; detecta pw-play, paplay, aplay ou ffplay
"""
from __future__ import annotations

import array
import math
import re
import shlex
import shutil
import subprocess
import tempfile
import time
import wave
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

_URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
_SYMBOLS_RE = re.compile(r"[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0000FE00-\U0000FE0F\U0001F1E6-\U0001F1FF]+")
_MARKUP_RE = re.compile(r"[*_`#>|~^\[\]{}<>\\]+")

PLAYER_CANDIDATES = ("pw-play {file}", "paplay {file}", "aplay -q {file}", "ffplay -nodisp -autoexit -loglevel quiet {file}")


def clean_for_speech(text: str, limit: int = 400) -> str:
    """Tira o que um sintetizador leria errado: links, emojis e marcação."""
    out = _URL_RE.sub(" ", str(text or ""))
    out = _SYMBOLS_RE.sub(" ", out)
    out = _MARKUP_RE.sub(" ", out)
    out = " ".join(out.replace("@", " ").split())
    return out[:limit].strip()


def wav_envelope(path: Path, fps: int = 30) -> Tuple[float, List[float]]:
    """Lê um WAV PCM e devolve (duração em s, abertura de boca de 0 a 1 por quadro)."""
    with wave.open(str(path), "rb") as wav:
        rate, width, total = wav.getframerate(), wav.getsampwidth(), wav.getnframes()
        channels = max(1, wav.getnchannels())
        raw = wav.readframes(total)
    if not rate or not total:
        return 0.0, []
    if width == 1:
        samples: Any = [b - 128 for b in raw]
        full_scale = 128.0
    elif width == 2:
        samples = array.array("h")
        samples.frombytes(raw[: len(raw) // 2 * 2])
        full_scale = 32768.0
    elif width == 4:
        samples = array.array("i")
        samples.frombytes(raw[: len(raw) // 4 * 4])
        full_scale = 2147483648.0
    else:                                   # 24 bits: usa os 2 bytes mais significativos de cada amostra
        samples = [int.from_bytes(raw[i + 1:i + 3], "little", signed=True) for i in range(0, len(raw) - 2, 3)]
        full_scale = 32768.0
    window = max(1, int(rate * channels / fps))
    step = max(1, window // 256)            # subamostra: basta para o envelope e mantém o cálculo rápido
    levels: List[float] = []
    for start in range(0, len(samples), window):
        chunk = samples[start:start + window:step]
        if not chunk:
            break
        levels.append(math.sqrt(sum(s * s for s in chunk) / len(chunk)) / full_scale)
    peak = max(levels) if levels else 0.0
    if peak <= 1e-6:
        return total / rate, [0.0] * len(levels)
    mouth: List[float] = []
    previous = 0.0
    for level in levels:
        value = min(1.0, (level / peak) ** 0.6)
        value = 0.0 if value < 0.08 else value
        previous = 0.55 * value + 0.45 * previous       # suaviza para a boca não tremer
        mouth.append(round(previous, 3))
    return total / rate, mouth


class SpeechEngine:
    def __init__(self, tts_command: str, player_command: str = "", work_dir: Optional[Path] = None,
                 tts_timeout_s: float = 60.0, fps: int = 30):
        self.tts_args = shlex.split(tts_command or "")
        if self.tts_args and not any("{out}" in a for a in self.tts_args):
            raise ValueError("BYTE_TTS_COMMAND precisa conter {out} (o arquivo WAV a ser gerado).")
        self.text_in_args = any("{text}" in a for a in self.tts_args)
        self.player_args = self._resolve_player(player_command)
        self.work_dir = Path(work_dir) if work_dir else Path(tempfile.mkdtemp(prefix="byte_tts_"))
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.tts_timeout_s = float(tts_timeout_s)
        self.fps = int(fps)
        self.last_error: Optional[str] = None

    @staticmethod
    def _resolve_player(player_command: str) -> List[str]:
        if player_command.strip().lower() == "none":
            return []                                    # só anima a boca (o áudio é tocado por outro programa)
        if player_command.strip():
            return shlex.split(player_command)
        for candidate in PLAYER_CANDIDATES:
            args = shlex.split(candidate)
            if shutil.which(args[0]):
                return args
        return []

    @property
    def available(self) -> bool:
        return bool(self.tts_args) and shutil.which(self.tts_args[0]) is not None

    def synthesize(self, text: str) -> Path:
        spoken = clean_for_speech(text)
        if not spoken:
            raise ValueError("Nada para falar.")
        out = self.work_dir / f"fala_{int(time.time() * 1000)}.wav"
        args = [a.replace("{out}", str(out)).replace("{text}", spoken) for a in self.tts_args]
        proc = subprocess.run(args, input=None if self.text_in_args else spoken.encode("utf-8"),
                              stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=self.tts_timeout_s,
                              stdin=subprocess.DEVNULL if self.text_in_args else None)
        if proc.returncode != 0 or not out.exists() or out.stat().st_size < 64:
            raise RuntimeError(f"O TTS falhou (código {proc.returncode}): {proc.stderr.decode('utf-8', 'replace')[-200:]}")
        return out

    def speak(self, text: str, on_mouth: Optional[Callable[[float], None]] = None) -> Dict[str, Any]:
        """Sintetiza, toca e chama on_mouth(valor 0..1) no ritmo do áudio. Bloqueia até a fala terminar."""
        wav_path: Optional[Path] = None
        player: Optional[subprocess.Popen] = None
        try:
            wav_path = self.synthesize(text)
            duration, mouth = wav_envelope(wav_path, fps=self.fps)
            if self.player_args:
                args = [a.replace("{file}", str(wav_path)) for a in self.player_args]
                if not any("{file}" in a for a in self.player_args):
                    args.append(str(wav_path))
                player = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            started = time.monotonic()
            mouth_ok = on_mouth is not None
            for index, value in enumerate(mouth):
                delay = started + index / self.fps - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
                if mouth_ok:
                    try:
                        on_mouth(value)
                    except Exception:
                        mouth_ok = False             # avatar indisponível: segue só com o áudio
            remaining = started + duration - time.monotonic()
            if remaining > 0:
                time.sleep(remaining)
            if on_mouth is not None:
                try:
                    on_mouth(0.0)
                except Exception:
                    pass
            if player is not None:
                try:
                    player.wait(timeout=5.0)
                except subprocess.TimeoutExpired:
                    player.kill()
            self.last_error = None
            return {"spoken": True, "duration_s": round(duration, 2), "audio": bool(self.player_args)}
        except Exception as err:
            if player is not None and player.poll() is None:
                player.kill()
            self.last_error = f"{type(err).__name__}: {str(err)[:200]}"
            return {"spoken": False, "error": self.last_error}
        finally:
            if wav_path is not None:
                try:
                    wav_path.unlink()
                except OSError:
                    pass

"""Comando de voz para todos os agentes (v12.3).

Três peças, todas opcionais e configuráveis por variável de ambiente ou segredo do Colab:

* Transcrição (fala -> texto), para áudios do Telegram, da página /voz e do painel sem reconhecimento no navegador:
    - API compatível com OpenAI (/audio/transcriptions): Groq (camada gratuita), OpenAI, faster-whisper-server, LocalAI...
          BYTE_STT_URL=https://api.groq.com/openai/v1   BYTE_STT_MODEL=whisper-large-v3-turbo   segredo BYTE_STT_API_KEY
    - faster-whisper instalado no mesmo Python (pip install faster-whisper): roda local, na GPU do Colab se houver.
          BYTE_STT_MODEL=small (padrão)
* Fala das respostas (texto -> áudio), para responder com áudio no Telegram e na página /voz:
    - API compatível com OpenAI (/audio/speech): Kokoro-FastAPI (tem voz pt-BR), OpenAI...
          BYTE_VOICE_TTS_URL=http://127.0.0.1:8880/v1   BYTE_VOICE_TTS_VOICE=pf_dora   segredo BYTE_VOICE_TTS_API_KEY
    - ou o mesmo comando local da live (BYTE_TTS_COMMAND, ex.: Piper).
  O painel e a página /voz também podem falar com a voz do próprio navegador/celular, sem servidor de TTS.
* Regras por agente (campo "voz" do manifesto): quem pode falar, palavra de ativação e quando responder com voz.
    - Byte (VTuber): só o dono. No Telegram, só no chat PRIVADO do dono; na live, a voz do dono vira uma fala
      dela na live, respondendo a ele (não ao chat). No modo mãos livres ela só reage quando chamam "Byte".
    - Kronus: voz é o canal principal: respostas curtas para relógio/celular e modo mãos livres com "Kronus".
    - Matrix e Bit: comando de voz normal, respostas faladas mais completas (o Bit resume e deixa o detalhe no texto).

Por segurança, voz nunca aprova ações externas (e-mail, Asana, Notion, Teams): aprovação continua nos botões.
"""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

MAX_AUDIO_BYTES = 15 * 1024 * 1024
RESPONSE_MODES = ("auto", "sempre", "nunca")

VOICE_DEFAULTS: Dict[str, Dict[str, Any]] = {
    # assistente (Kronus): a voz é a peça central; respostas curtas e mãos livres
    "assistente": {"ativo": True, "responder_com_voz": "auto", "palavras_chave": [], "somente_dono": False, "na_live": False},
    "trabalho": {"ativo": True, "responder_com_voz": "auto", "palavras_chave": [], "somente_dono": False, "na_live": False},
    # vtuber (Byte): só o dono comanda por voz; durante a live, ela responde em voz alta na própria live
    "vtuber": {"ativo": True, "responder_com_voz": "auto", "palavras_chave": [], "somente_dono": True, "na_live": True},
}
MATRIX_VOICE = {"ativo": True, "responder_com_voz": "auto", "palavras_chave": ["matrix", "matriz"], "somente_dono": False,
                "na_live": False}

# como cada tipo de agente responde quando a pergunta chega por voz
VOICE_DIRECTIVES = {
    "assistente": ("A pergunta chegou por VOZ e a resposta será FALADA no relógio ou celular: no máximo 2 frases curtas, "
                   "sem listas, markdown, emojis, links ou blocos de código."),
    "trabalho": ("A pergunta chegou por VOZ e a resposta será ouvida: comece pela conclusão em até 4 frases, sem markdown, "
                 "emojis ou links. Se o assunto pedir números ou tabela, resuma em voz e diga que o detalhe está no texto."),
    "vtuber": ("Quem está falando por VOZ é o seu dono, não alguém do chat. A resposta será FALADA: 1 a 3 frases naturais, "
               "sem listas, markdown, emojis ou links."),
    "matrix": ("A pergunta chegou por VOZ e a resposta será ouvida: até 4 frases claras, sem markdown, emojis, links ou "
               "blocos de código."),
}


class VoiceError(RuntimeError):
    pass


def fold(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", str(text or "").lower()) if not unicodedata.combining(c))


# ---------------------------------------------------------------- regras por agente
def default_wake_words(name: str) -> List[str]:
    """Nome do agente + variações que a transcrição costuma produzir."""
    base = fold(name).strip()
    extra = {"kronus": ["cronus", "cronos", "kronos", "crono"], "byte": ["bait", "baite", "baiti"],
             "bit": ["biti"], "matrix": ["matriz"]}.get(base, [])
    return [w for w in [base] + extra if w]


def validate_voice(raw: Any, kind: str, name: str) -> Dict[str, Any]:
    """Normaliza o campo "voz" de um manifesto. Lança ValueError com mensagem em português."""
    base = dict(VOICE_DEFAULTS.get(kind, VOICE_DEFAULTS["assistente"]))
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError("'voz' deve ser um objeto (ativo, responder_com_voz, palavras_chave, somente_dono, na_live).")
    unknown = set(raw) - set(base)
    if unknown:
        raise ValueError(f"'voz' tem campos desconhecidos: {', '.join(sorted(unknown))}.")
    for key in ("ativo", "somente_dono", "na_live"):
        if key in raw:
            if not isinstance(raw[key], bool):
                raise ValueError(f"'voz.{key}' deve ser true ou false.")
            base[key] = raw[key]
    if "responder_com_voz" in raw:
        mode = str(raw["responder_com_voz"]).strip().lower()
        if mode not in RESPONSE_MODES:
            raise ValueError(f"'voz.responder_com_voz' deve ser um de: {', '.join(RESPONSE_MODES)}.")
        base["responder_com_voz"] = mode
    words = raw.get("palavras_chave", base["palavras_chave"])
    if isinstance(words, str):
        words = [words]
    if not isinstance(words, list) or not all(isinstance(w, str) for w in words):
        raise ValueError("'voz.palavras_chave' deve ser uma lista de palavras.")
    words = [fold(w).strip() for w in words if fold(w).strip()][:8]
    base["palavras_chave"] = words or default_wake_words(name)
    if base["na_live"] and kind != "vtuber":
        raise ValueError("'voz.na_live' só vale para agentes do tipo 'vtuber'.")
    return base


def policy_for(manifest: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not manifest:
        return dict(MATRIX_VOICE, tipo="matrix", nome="Matrix")
    pol = dict(manifest.get("voz") or validate_voice(None, manifest["tipo"], manifest["nome"]))
    pol.update(tipo=manifest["tipo"], nome=manifest["nome"])
    return pol


def match_wake_word(text: str, words: List[str]) -> Tuple[bool, str]:
    """Modo mãos livres: só reage se a frase chama o agente ("Kronus, que horas são?", "ei Byte, ...").
    Devolve (chamou?, comando sem a palavra de ativação)."""
    raw = " ".join(str(text or "").split())
    tokens = raw.split(" ")
    folded = [re.sub(r"[^\w]", "", fold(t)) for t in tokens]
    for idx, tok in enumerate(folded[:4]):              # o nome tem de vir no começo da frase (até a 4ª palavra)
        if tok in words:
            rest = " ".join(tokens[idx + 1:]).lstrip(" ,.:;!?-")
            return True, rest
    return False, raw


# ---------------------------------------------------------------- transcrição
_LOCAL_MODELS: Dict[str, Any] = {}
_LOCAL_LOCK = threading.Lock()


def _multipart(fields: Dict[str, str], file_field: str, filename: str, data: bytes, mime: str) -> Tuple[bytes, str]:
    boundary = "----bytevoz" + uuid.uuid4().hex
    out = []
    for key, value in fields.items():
        out.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode("utf-8"))
    out.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'
               f"Content-Type: {mime}\r\n\r\n".encode("utf-8") + data + b"\r\n")
    out.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(out), f"multipart/form-data; boundary={boundary}"


def _ext_for(mime: str) -> str:
    mime = (mime or "").split(";")[0].strip().lower()
    return {"audio/ogg": ".ogg", "audio/opus": ".ogg", "audio/webm": ".webm", "video/webm": ".webm", "audio/mpeg": ".mp3",
            "audio/mp3": ".mp3", "audio/mp4": ".m4a", "audio/x-m4a": ".m4a", "audio/aac": ".aac", "audio/wav": ".wav",
            "audio/x-wav": ".wav", "audio/wave": ".wav", "audio/flac": ".flac"}.get(mime, ".ogg")


class Transcriber:
    def __init__(self, url: str = "", model: str = "", api_key: str = "", language: str = "pt", backend: str = "auto",
                 timeout_s: float = 90.0):
        self.url = url.strip().rstrip("/")
        self.model = model.strip()
        self.api_key = api_key.strip()
        self.language = (language or "pt").strip()
        self.timeout_s = timeout_s
        self.last_error: Optional[str] = None
        backend = (backend or "auto").strip().lower()
        if backend == "auto":
            backend = "http" if self.url else ("local" if _faster_whisper_installed() else "")
        self.backend = backend

    @classmethod
    def from_env(cls, env: Any, read_secret: Optional[Callable[[Tuple[str, ...]], str]] = None) -> "Transcriber":
        read = read_secret or (lambda names: next((str(env.get(n, "")) for n in names if env.get(n)), ""))
        return cls(url=read(("BYTE_STT_URL",)), model=read(("BYTE_STT_MODEL",)), api_key=read(("BYTE_STT_API_KEY",)),
                   language=read(("BYTE_STT_LANG",)) or "pt", backend=read(("BYTE_STT_BACKEND",)) or "auto")

    @property
    def available(self) -> bool:
        return self.backend in {"http", "local"}

    def describe(self) -> Optional[str]:
        if self.backend == "http":
            return f"API ({urllib.parse.urlsplit(self.url).netloc or self.url})"
        if self.backend == "local":
            return f"faster-whisper ({self.model or 'small'})"
        return None

    def transcribe(self, audio: bytes, mime: str = "audio/ogg") -> str:
        if not audio:
            raise VoiceError("Áudio vazio.")
        if len(audio) > MAX_AUDIO_BYTES:
            raise VoiceError("Áudio grande demais (limite de 15 MB).")
        if self.backend == "http":
            return self._http(audio, mime)
        if self.backend == "local":
            return self._local(audio, mime)
        raise VoiceError("Transcrição de voz não configurada: defina BYTE_STT_URL (ex.: Groq, gratuito) ou instale faster-whisper. "
                         "Guia: docs/voz.md")

    def _http(self, audio: bytes, mime: str) -> str:
        endpoint = self.url if self.url.endswith("/audio/transcriptions") else self.url + "/audio/transcriptions"
        body, ctype = _multipart({"model": self.model or "whisper-1", "language": self.language, "response_format": "json"},
                                 "file", "voz" + _ext_for(mime), audio, (mime or "audio/ogg").split(";")[0])
        headers = {"Content-Type": ctype}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        req = urllib.request.Request(endpoint, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                data = json.loads(resp.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as err:
            detail = err.read(300).decode("utf-8", "replace")
            self.last_error = f"HTTP {err.code}"
            raise VoiceError(f"O serviço de transcrição recusou o áudio (HTTP {err.code}): {detail[:200]}") from None
        except (urllib.error.URLError, OSError, ValueError) as err:
            self.last_error = type(err).__name__
            raise VoiceError(f"Serviço de transcrição indisponível: {type(err).__name__}") from None
        return " ".join(str(data.get("text") or "").split())

    def _local(self, audio: bytes, mime: str) -> str:
        from faster_whisper import WhisperModel  # type: ignore
        name = self.model or "small"
        with _LOCAL_LOCK:
            model = _LOCAL_MODELS.get(name)
            if model is None:
                model = _LOCAL_MODELS[name] = WhisperModel(name, device="auto", compute_type="default")
        with tempfile.NamedTemporaryFile(suffix=_ext_for(mime), delete=False) as tmp:
            tmp.write(audio)
            path = tmp.name
        try:
            segments, _ = model.transcribe(path, language=self.language[:2], vad_filter=True)
            return " ".join(" ".join(seg.text for seg in segments).split())
        finally:
            os.unlink(path)


def _faster_whisper_installed() -> bool:
    try:
        import importlib.util
        return importlib.util.find_spec("faster_whisper") is not None
    except (ImportError, ValueError):
        return False


# ---------------------------------------------------------------- fala das respostas
_URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
_FENCE_RE = re.compile(r"```.*?(```|\Z)", re.DOTALL)
_EMOJI_RE = re.compile(r"[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0000FE00-\U0000FE0F\U0001F1E6-\U0001F1FF]+")


def speech_text(reply: str, limit: int = 700) -> str:
    """Texto que vai para o sintetizador: sem blocos de código, cartões, links, emojis e marcação."""
    out = _FENCE_RE.sub(" ", str(reply or ""))
    out = re.sub(r"🛡️[^\n]*\n?", " ", out)
    out = _URL_RE.sub(" ", out)
    out = _EMOJI_RE.sub(" ", out)
    out = re.sub(r"[*_`#>|~\[\]{}<\\]+", " ", out)
    out = " ".join(out.split())
    if len(out) > limit:
        cut = out[:limit]
        end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
        out = cut[:end + 1] if end > limit // 3 else cut.rstrip() + "…"
    return out


class Synthesizer:
    def __init__(self, url: str = "", model: str = "", voice: str = "", api_key: str = "", command: str = "",
                 timeout_s: float = 60.0):
        self.url = url.strip().rstrip("/")
        self.model = model.strip()
        self.voice = voice.strip()
        self.api_key = api_key.strip()
        self.command = command.strip()
        self.timeout_s = timeout_s
        self.backend = "http" if self.url else ("command" if self.command else "")

    @classmethod
    def from_env(cls, env: Any, read_secret: Optional[Callable[[Tuple[str, ...]], str]] = None) -> "Synthesizer":
        read = read_secret or (lambda names: next((str(env.get(n, "")) for n in names if env.get(n)), ""))
        return cls(url=read(("BYTE_VOICE_TTS_URL",)), model=read(("BYTE_VOICE_TTS_MODEL",)), voice=read(("BYTE_VOICE_TTS_VOICE",)),
                   api_key=read(("BYTE_VOICE_TTS_API_KEY",)), command=read(("BYTE_TTS_COMMAND",)))

    @property
    def available(self) -> bool:
        return bool(self.backend)

    def describe(self) -> Optional[str]:
        return {"http": "API de voz", "command": "comando local (BYTE_TTS_COMMAND)"}.get(self.backend)

    def synthesize(self, text: str) -> Tuple[bytes, str]:
        """Devolve (áudio, mime). Prefere OGG/Opus (nota de voz do Telegram) e cai para MP3/WAV."""
        text = speech_text(text)
        if not text:
            raise VoiceError("Nada para falar.")
        if self.backend == "http":
            return self._http(text)
        if self.backend == "command":
            return self._command(text)
        raise VoiceError("Fala das respostas não configurada (BYTE_VOICE_TTS_URL ou BYTE_TTS_COMMAND).")

    def _http(self, text: str) -> Tuple[bytes, str]:
        endpoint = self.url if self.url.endswith("/audio/speech") else self.url + "/audio/speech"
        payload = {"model": self.model or "tts-1", "input": text, "voice": self.voice or "alloy", "response_format": "opus"}
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        for fmt, mime in (("opus", "audio/ogg"), ("mp3", "audio/mpeg")):
            payload["response_format"] = fmt
            req = urllib.request.Request(endpoint, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                    data = resp.read(MAX_AUDIO_BYTES + 1)
                if data and len(data) <= MAX_AUDIO_BYTES:
                    return data, mime
            except urllib.error.HTTPError as err:
                if err.code in (400, 422) and fmt == "opus":
                    continue                                # servidor sem opus: tenta mp3
                raise VoiceError(f"O serviço de voz recusou o pedido (HTTP {err.code}).") from None
            except (urllib.error.URLError, OSError) as err:
                raise VoiceError(f"Serviço de voz indisponível: {type(err).__name__}") from None
        raise VoiceError("O serviço de voz não devolveu áudio.")

    def _command(self, text: str) -> Tuple[bytes, str]:
        from .speech import SpeechEngine
        wav = SpeechEngine(self.command).synthesize(text)
        try:
            ffmpeg = shutil.which("ffmpeg")
            if ffmpeg:                                       # nota de voz do Telegram pede OGG/Opus
                ogg = wav.with_suffix(".ogg")
                proc = subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-i", str(wav), "-c:a", "libopus", "-b:a", "32k", str(ogg)],
                                      capture_output=True, timeout=60)
                if proc.returncode == 0 and ogg.exists():
                    data = ogg.read_bytes()
                    ogg.unlink()
                    return data, "audio/ogg"
            return wav.read_bytes(), "audio/wav"
        finally:
            try:
                wav.unlink()
            except OSError:
                pass


# ---------------------------------------------------------------- comando de voz (usado pelo motor)
class VoiceKit:
    def __init__(self, transcriber: Transcriber, synthesizer: Synthesizer):
        self.transcriber = transcriber
        self.synthesizer = synthesizer

    @classmethod
    def from_env(cls, env: Any = None, read_secret: Optional[Callable[[Tuple[str, ...]], str]] = None) -> "VoiceKit":
        env = os.environ if env is None else env
        return cls(Transcriber.from_env(env, read_secret), Synthesizer.from_env(env, read_secret))

    def status(self) -> Dict[str, Any]:
        return {"stt": self.transcriber.describe(), "tts": self.synthesizer.describe()}


def _decode_audio(payload: Dict[str, Any]) -> Tuple[bytes, str]:
    raw = payload.get("audio_b64")
    if isinstance(raw, (bytes, bytearray)):
        return bytes(raw), str(payload.get("mime") or "audio/ogg")
    try:
        data = base64.b64decode(str(raw or "").split(",", 1)[-1], validate=False)
    except (ValueError, TypeError):
        raise VoiceError("Áudio inválido (base64).") from None
    return data, str(payload.get("mime") or "audio/ogg")


def handle_command(service: Any, payload: Dict[str, Any], kit: VoiceKit,
                   interact_kwargs: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Um comando de voz: texto já reconhecido (navegador/celular) ou áudio (Telegram, /voz) -> resposta do agente.

    payload: text | audio_b64 + mime; source (painel, telegram, api); owner (o canal confirmou que é o dono);
             hands_free (exige palavra de ativação); audio_reply (pede a resposta em áudio).
    """
    from . import engine
    policy = policy_for(engine.ACTIVE_AGENT)
    if not policy["ativo"]:
        raise VoiceError(f"O comando de voz está desligado para {policy['nome']}.")
    if policy["somente_dono"] and not payload.get("owner", False):
        raise PermissionError(f"{policy['nome']} só aceita comando de voz do dono (chat privado do Telegram, painel ou página de voz).")
    came_as_audio = bool(payload.get("audio_b64"))
    if came_as_audio:
        audio, mime = _decode_audio(payload)
        heard = kit.transcriber.transcribe(audio, mime)
    else:
        heard = " ".join(str(payload.get("text") or "").split())
    heard = heard[:2000]
    out: Dict[str, Any] = {"heard": heard, "agent": engine.AGENT_NAME, "voice_policy": {
        k: policy[k] for k in ("responder_com_voz", "palavras_chave", "somente_dono", "na_live")}}
    if not heard:
        out.update(ignored=True, reason="Não entendi o áudio.")
        return out
    command = heard
    if payload.get("hands_free"):
        called, command = match_wake_word(heard, policy["palavras_chave"])
        if not called:
            out.update(ignored=True, reason="Sem a palavra de ativação.")
            return out
        if not command:
            out.update(wake_only=True)                      # só chamou o nome: o cliente volta a ouvir o comando
            return out
    live = getattr(service, "live_owner_sink", None)
    if policy.get("na_live") and callable(live):
        res = live(command)                                 # a Byte responde em voz alta NA LIVE, falando com o dono
        out.update(res or {})
        out.update(live=True, response=str((res or {}).get("note") or "Respondendo na live."), provider_used="LIVE")
        return out
    directive = VOICE_DIRECTIVES.get(policy["tipo"], VOICE_DIRECTIVES["matrix"])
    kwargs = dict(interact_kwargs or {})
    kwargs["extra_directive"] = directive
    res = service.interact(command, **kwargs)
    out = {**res, **out}
    out["speech"] = speech_text(str(res.get("response") or ""))
    mode = policy["responder_com_voz"]
    wants_audio = payload.get("audio_reply")
    if wants_audio is None:
        wants_audio = mode == "sempre" or (mode == "auto" and came_as_audio)
    if mode == "nunca":
        wants_audio = False
    if wants_audio and kit.synthesizer.available and out["speech"]:
        try:
            data, mime = kit.synthesizer.synthesize(out["speech"])
            out["audio_b64"], out["audio_mime"] = base64.b64encode(data).decode("ascii"), mime
        except Exception as err:                            # sem áudio, a resposta em texto continua valendo
            out["audio_error"] = f"{type(err).__name__}: {str(err)[:160]}"
    return out

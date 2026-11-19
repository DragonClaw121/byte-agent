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
    - ou o Azure AI Speech (v13.7; plano gratuito F0 com vozes pt-BR neurais), bom para a Byte na nuvem:
          AZURE_SPEECH_REGION=brazilsouth   segredo AZURE_SPEECH_KEY   (BYTE_VOICE_TTS_VOICE=pt-BR-FranciscaNeural opcional)
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
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

MAX_AUDIO_BYTES = 15 * 1024 * 1024
# v12.7: APIs atrás do Cloudflare (Groq, OpenAI) recusam o identificador padrão do Python ("Python-urllib") com
# HTTP 403 / error code 1010. Toda chamada sai com um identificador genérico do projeto.
USER_AGENT = "byte-agent-voz/1.0"
RESPONSE_MODES = ("auto", "sempre", "nunca")

# v12.4: cada agente tem a própria voz. "voz_servidor" é o nome da voz no serviço de TTS (ex.: Kokoro pf_dora);
# "tom" e "velocidade" (0.5 a 2.0) valem para a voz do navegador/celular, e "velocidade" também para o servidor.
_VOICE_LOOK = {"voz_servidor": "", "tom": 1.0, "velocidade": 1.0, "estilo": "", "intensidade": 1.0}
VOICE_DEFAULTS: Dict[str, Dict[str, Any]] = {
    # assistente (Kronus): a voz é a peça central; respostas curtas e mãos livres
    "assistente": {"ativo": True, "responder_com_voz": "auto", "palavras_chave": [], "somente_dono": False, "na_live": False, **_VOICE_LOOK},
    "trabalho": {"ativo": True, "responder_com_voz": "auto", "palavras_chave": [], "somente_dono": False, "na_live": False, **_VOICE_LOOK},
    # vtuber (Byte): só o dono comanda por voz; durante a live, ela responde em voz alta na própria live
    "vtuber": {"ativo": True, "responder_com_voz": "auto", "palavras_chave": [], "somente_dono": True, "na_live": True,
               **dict(_VOICE_LOOK, estilo="cheerful", intensidade=1.3)},       # v13.13: alegre e doce, não competitiva
}
# Matrix: voz do servidor = BYTE_VOICE_TTS_VOICE (a voz padrão); serena, um pouco mais pausada
MATRIX_VOICE = {"ativo": True, "responder_com_voz": "auto", "palavras_chave": ["matrix", "matriz"], "somente_dono": False,
                "na_live": False, "voz_servidor": "", "tom": 1.0, "velocidade": 1.1, "estilo": "", "intensidade": 1.0}

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


def _retry_pause(seconds: float) -> None:
    time.sleep(seconds)


class _RetryableVoiceError(VoiceError):
    def __init__(self, message: str, wait_s: float = 0.0):
        super().__init__(message)
        self.wait_s = wait_s


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
    if "voz_servidor" in raw:
        name_v = str(raw["voz_servidor"] or "").strip()
        if not re.fullmatch(r"[\w.+:-]{0,60}", name_v):
            raise ValueError("'voz.voz_servidor' deve ser o nome de uma voz do serviço de TTS (ex.: pf_dora).")
        base["voz_servidor"] = name_v
    for key in ("tom", "velocidade"):
        if key in raw:
            try:
                value = float(raw[key])
            except (TypeError, ValueError):
                raise ValueError(f"'voz.{key}' deve ser um número entre 0.5 e 2.0.") from None
            if not 0.5 <= value <= 2.0:
                raise ValueError(f"'voz.{key}' deve ficar entre 0.5 e 2.0.")
            base[key] = round(value, 2)
    if "estilo" in raw:
        style = str(raw["estilo"] or "").strip().lower()
        if not re.fullmatch(r"[a-z-]{0,30}", style):
            raise ValueError("'voz.estilo' deve ser um estilo do Azure (ex.: cheerful, affectionate, gentle) ou vazio.")
        base["estilo"] = style
    if "intensidade" in raw:
        try:
            degree = float(raw["intensidade"])
        except (TypeError, ValueError):
            raise ValueError("'voz.intensidade' deve ser um número entre 0.1 e 2.0.") from None
        if not 0.1 <= degree <= 2.0:
            raise ValueError("'voz.intensidade' deve ficar entre 0.1 e 2.0.")
        base["intensidade"] = round(degree, 2)
    if base["na_live"] and kind != "vtuber":
        raise ValueError("'voz.na_live' só vale para agentes do tipo 'vtuber'.")
    return base


def _env_voice(name: str) -> str:
    """v13.7: BYTE_VOZ_<AGENTE> (ex.: BYTE_VOZ_KRONUS=pt-BR-DonatoNeural) troca a voz sem editar o manifesto."""
    key = "BYTE_VOZ_" + re.sub(r"[^A-Z0-9]", "_", fold(name).upper())
    value = os.environ.get(key, "").strip()
    return value if re.fullmatch(r"[A-Za-z0-9_.-]{2,80}", value) else ""


def policy_for(manifest: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not manifest:
        pol = dict(MATRIX_VOICE, tipo="matrix", nome="Matrix")
    else:
        pol = dict(manifest.get("voz") or validate_voice(None, manifest["tipo"], manifest["nome"]))
        pol.update(tipo=manifest["tipo"], nome=manifest["nome"])
    override = _env_voice(pol["nome"])
    if override:
        pol["voz_servidor"] = override
    speed = _env_speed(pol["nome"])
    if speed:
        pol["velocidade"] = speed
    pol.setdefault("estilo", VOICE_DEFAULTS.get(pol.get("tipo"), {}).get("estilo", ""))
    pol.setdefault("intensidade", 1.0)
    key = "BYTE_VOZ_" + re.sub(r"[^A-Z0-9]", "_", fold(pol["nome"]).upper())
    style = os.environ.get(key + "_ESTILO")
    if style is not None and re.fullmatch(r"[a-zA-Z-]{0,30}", style.strip()):
        pol["estilo"] = style.strip().lower()                  # vazio = sem estilo
    try:
        degree = float(os.environ.get(key + "_INTENSIDADE", "").replace(",", ".") or 0)
    except ValueError:
        degree = 0
    if degree:
        pol["intensidade"] = round(min(2.0, max(0.1, degree)), 2)
    return pol


def _env_speed(name: str) -> Optional[float]:
    """v13.11: BYTE_VOZ_<AGENTE>_VELOCIDADE (0.5 a 2.0), ex.: BYTE_VOZ_MATRIX_VELOCIDADE=1.15."""
    key = "BYTE_VOZ_" + re.sub(r"[^A-Z0-9]", "_", fold(name).upper()) + "_VELOCIDADE"
    try:
        value = float(os.environ.get(key, "").replace(",", ".") or 0)
    except ValueError:
        return None
    return round(min(2.0, max(0.5, value)), 2) if value else None


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


DEFAULT_STT_PROMPT = "Kronus, Matrix, Byte, Bit."


def stt_prompt() -> str:
    """Dica de vocabulário para o Whisper (BYTE_STT_PROMPT; vazio desliga). Melhora nomes como "Kronus"."""
    value = os.environ.get("BYTE_STT_PROMPT")
    return DEFAULT_STT_PROMPT if value is None else " ".join(value.split())[:200]


def echoes_prompt(text: str) -> bool:
    """Com áudio quase mudo o Whisper às vezes devolve a própria dica: isso não é fala de verdade."""
    prompt = stt_prompt()
    if not prompt or not text:
        return False
    norm = lambda t: re.sub(r"[^a-z0-9]+", " ", fold(t)).strip()
    return norm(text) == norm(prompt)


class Transcriber:
    def __init__(self, url: str = "", model: str = "", api_key: str = "", language: str = "pt", backend: str = "auto",
                 timeout_s: float = 40.0):
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
        try:
            timeout_s = float(read(("BYTE_STT_TIMEOUT_S",)) or 40)
        except ValueError:
            timeout_s = 40.0
        return cls(url=read(("BYTE_STT_URL",)), model=read(("BYTE_STT_MODEL",)), api_key=read(("BYTE_STT_API_KEY",)),
                   language=read(("BYTE_STT_LANG",)) or "pt", backend=read(("BYTE_STT_BACKEND",)) or "auto", timeout_s=timeout_s)

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
        if self.backend in {"http", "local"}:
            text = self._http(audio, mime) if self.backend == "http" else self._local(audio, mime)
            return "" if echoes_prompt(text) else text
        raise VoiceError("Transcrição de voz não configurada: defina BYTE_STT_URL (ex.: Groq, gratuito) ou instale faster-whisper. "
                         "Guia: docs/voz.md")

    def _http(self, audio: bytes, mime: str) -> str:
        """v12.9: até 3 tentativas. Rede instável (Wi-Fi, VPN, WSL) e limites momentâneos do serviço (429/5xx) faziam a
        transcrição estourar o tempo; erros definitivos (chave, permissão, tamanho) não são repetidos."""
        last: Optional[Exception] = None
        for attempt in range(3):
            try:
                return self._http_once(audio, mime)
            except _RetryableVoiceError as err:
                last = err
                if attempt < 2:
                    _retry_pause(min(10.0, err.wait_s or (2.0 * (attempt + 1))))
        raise VoiceError(f"{last} (depois de 3 tentativas). Se acontecer sempre, teste a rede até o serviço de transcrição.")

    def _http_once(self, audio: bytes, mime: str) -> str:
        endpoint = self.url if self.url.endswith("/audio/transcriptions") else self.url + "/audio/transcriptions"
        fields = {"model": self.model or "whisper-1", "language": self.language, "response_format": "json"}
        prompt = stt_prompt()
        if prompt:
            fields["prompt"] = prompt                      # v13.19: vocabulário esperado (nomes dos agentes)
        body, ctype = _multipart(fields,
                                 "file", "voz" + _ext_for(mime), audio, (mime or "audio/ogg").split(";")[0])
        headers = {"Content-Type": ctype, "User-Agent": USER_AGENT, "Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        req = urllib.request.Request(endpoint, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                data = json.loads(resp.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as err:
            detail = err.read(300).decode("utf-8", "replace")
            self.last_error = f"HTTP {err.code}"
            if err.code == 429 or err.code >= 500:
                try:
                    wait = float(err.headers.get("Retry-After") or 0)
                except (TypeError, ValueError):
                    wait = 0.0
                raise _RetryableVoiceError(f"O serviço de transcrição está ocupado (HTTP {err.code})", wait) from None
            hint = {401: " Confira a chave (BYTE_STT_API_KEY).", 403: " Acesso bloqueado pelo serviço (chave sem permissão ou rede bloqueada).",
                    413: " Áudio grande demais para o serviço.", 429: " Limite de uso do serviço atingido; tente daqui a pouco."}.get(err.code, "")
            raise VoiceError(f"O serviço de transcrição recusou o áudio (HTTP {err.code}).{hint} Detalhe: {detail[:160]}") from None
        except (urllib.error.URLError, OSError) as err:
            self.last_error = type(err).__name__
            reason = getattr(err, "reason", err)
            raise _RetryableVoiceError(f"A transcrição não respondeu a tempo ou a conexão caiu ({type(reason).__name__})") from None
        except ValueError:
            raise VoiceError("O serviço de transcrição devolveu uma resposta inválida.") from None
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
_TAG_RE = re.compile(r"\[[^\]\n]{1,24}\]")
_EMOJI_RE = re.compile(r"[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0000FE00-\U0000FE0F\U0001F1E6-\U0001F1FF]+")


def speech_text(reply: str, limit: int = 700) -> str:
    """Texto que vai para o sintetizador: sem blocos de código, cartões, links, emojis e marcação."""
    out = _FENCE_RE.sub(" ", str(reply or ""))
    out = re.sub(r"🛡️[^\n]*\n?", " ", out)
    out = _TAG_RE.sub(" ", out)                              # [feliz], [animada]: comandos do avatar, não fala
    out = _URL_RE.sub(" ", out)
    out = _EMOJI_RE.sub(" ", out)
    out = re.sub(r"[*_`#>|~\[\]{}<\\]+", " ", out)
    out = " ".join(out.split())
    if len(out) > limit:
        cut = out[:limit]
        end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
        out = cut[:end + 1] if end > limit // 3 else cut.rstrip() + "…"
    return out


# v13.7: Azure AI Speech. Os manifestos trazem vozes do Kokoro; com o Azure, cada uma vira uma voz pt-BR equivalente.
AZURE_DEFAULT_VOICE = "pt-BR-FranciscaNeural"
KOKORO_TO_AZURE = {
    "pf_dora": "pt-BR-ThalitaNeural",       # Byte: feminina, animada
    "pm_alex": "pt-BR-AntonioNeural",       # Bit: masculina, sóbria
    "pm_santa": "pt-BR-ValerioNeural",      # Kronus: masculina, grave
}
_AZURE_VOICE_RE = re.compile(r"^[a-z]{2,3}-[A-Z]{2}-[A-Za-z0-9]+Neural$")


# v13.11: como uma voz pt-BR deve ler termos em inglês comuns em live, games e trabalho (o texto escrito não muda).
# Acrescente os seus com BYTE_VOZ_PRONUNCIAS="palavra=como se fala;outra=como se fala".
PRONUNCIATIONS = {
    "byte": "báite", "live": "laive", "lives": "laives", "stream": "strim", "streams": "strims", "streamer": "strímer",
    "streamers": "strímers", "streaming": "strímin", "game": "gueime", "games": "gueimes", "gamer": "gueimer",
    "gameplay": "gueimplei", "vtuber": "vi tiúber", "vtubers": "vi tiúbers", "twitch": "tuítch", "youtube": "iutúbi",
    "online": "onlaine", "offline": "oflaine", "download": "dáunloud", "upload": "âploud", "feedback": "fídbéque",
    "setup": "sétâp", "play": "plêi", "player": "plêier", "players": "plêiers", "boss": "bós", "skin": "isquin",
    "skins": "isquins", "spoiler": "ispóiler", "hype": "ráipe", "nice": "náice", "gg": "gê gê", "ok": "oquêi",
    "okay": "oquêi", "e-mail": "imêiu", "email": "imêiu", "e-mails": "imêius", "emails": "imêius", "mouse": "máuse",
    "software": "sóftuer", "hardware": "rárduer", "wi-fi": "uai fai", "wifi": "uai fai", "discord": "díscord",
    "minecraft": "máinecraft", "chat": "chét", "lag": "lég", "nerf": "nérf", "buff": "bâf", "headset": "réd sét",
    "webcam": "uébi quém", "insight": "ínsait", "insights": "ínsaits", "deadline": "dédlaine", "meeting": "mítin",
    "dashboard": "déshbord", "report": "ripórt", "slides": "isláides", "google": "gúgou", "drive": "dráive",
}


def _pronunciations() -> Dict[str, str]:
    lex = dict(PRONUNCIATIONS)
    for item in os.environ.get("BYTE_VOZ_PRONUNCIAS", "").split(";"):
        word, _, said = item.partition("=")
        word, said = word.strip().lower(), " ".join(said.split())
        if word and said and len(word) <= 40 and len(said) <= 60:
            lex[word] = said
    return lex


def apply_pronunciations(escaped_text: str) -> str:
    """Troca termos conhecidos por <sub alias>, que a voz lê do jeito certo (o texto já vem escapado para XML)."""
    lex = _pronunciations()
    words = sorted(lex, key=len, reverse=True)
    pattern = re.compile(r"(?<![\w-])(" + "|".join(re.escape(w) for w in words) + r")(?![\w-])", re.IGNORECASE)
    return pattern.sub(lambda m: f'<sub alias="{_xml(lex[m.group(1).lower()])}">{m.group(1)}</sub>', escaped_text)


def _xml(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;").replace("'", "&apos;"))


class Synthesizer:
    def __init__(self, url: str = "", model: str = "", voice: str = "", api_key: str = "", command: str = "",
                 timeout_s: float = 60.0, azure_key: str = "", azure_region: str = "", azure_endpoint: str = ""):
        self.url = url.strip().rstrip("/")
        self.model = model.strip()
        self.voice = voice.strip()
        self.api_key = api_key.strip()
        self.command = command.strip()
        self.timeout_s = timeout_s
        self.azure_key = azure_key.strip()
        region = re.sub(r"[^a-z0-9]", "", azure_region.strip().lower())
        self.azure_endpoint = (azure_endpoint.strip() or
                               (f"https://{region}.tts.speech.microsoft.com/cognitiveservices/v1" if region else ""))
        if self.url:
            self.backend = "http"
        elif self.azure_key and self.azure_endpoint:
            self.backend = "azure"
        else:
            self.backend = "command" if self.command else ""
        self.last_voice_error: Optional[str] = None

    @classmethod
    def from_env(cls, env: Any, read_secret: Optional[Callable[[Tuple[str, ...]], str]] = None) -> "Synthesizer":
        read = read_secret or (lambda names: next((str(env.get(n, "")) for n in names if env.get(n)), ""))
        return cls(url=read(("BYTE_VOICE_TTS_URL",)), model=read(("BYTE_VOICE_TTS_MODEL",)), voice=read(("BYTE_VOICE_TTS_VOICE",)),
                   api_key=read(("BYTE_VOICE_TTS_API_KEY",)), command=read(("BYTE_TTS_COMMAND",)),
                   azure_key=read(("AZURE_SPEECH_KEY",)), azure_region=read(("AZURE_SPEECH_REGION",)),
                   azure_endpoint=read(("AZURE_SPEECH_ENDPOINT",)))

    @property
    def available(self) -> bool:
        return bool(self.backend)

    def describe(self) -> Optional[str]:
        return {"http": "API de voz", "azure": "Azure AI Speech",
                "command": "comando local (BYTE_TTS_COMMAND)"}.get(self.backend)

    def synthesize(self, text: str, voice: Optional[str] = None, speed: Optional[float] = None,
                   style: Optional[str] = None, style_degree: Optional[float] = None) -> Tuple[bytes, str]:
        """Devolve (áudio, mime). Prefere OGG/Opus (nota de voz do Telegram) e cai para MP3/WAV.
        voice/speed: a voz do agente ativo; se o serviço não conhecer essa voz, usa a voz padrão."""
        text = speech_text(text)
        if not text:
            raise VoiceError("Nada para falar.")
        if self.backend == "http":
            if voice and voice != self.voice:
                try:
                    return self._http(text, voice, speed)
                except VoiceError as err:
                    if "HTTP 4" not in str(err):
                        raise
                    self.last_voice_error = f"voz '{voice}' recusada pelo serviço; usando a padrão"
            return self._http(text, None, speed)
        if self.backend == "azure":
            default = self.azure_voice(None)
            wanted = self.azure_voice(voice)
            # tenta: voz do agente com estilo -> mesma voz sem estilo -> voz padrão
            attempts = []
            if style:
                attempts.append((wanted, style))
            attempts.append((wanted, None))
            if wanted != default:
                attempts.append((default, None))
            for i, (v, st) in enumerate(attempts):
                try:
                    return self._azure(text, v, speed, st, style_degree)
                except VoiceError as err:
                    last = i == len(attempts) - 1
                    if last or "HTTP 4" not in str(err) or "HTTP 401" in str(err) or "HTTP 403" in str(err):
                        raise
                    self.last_voice_error = (f"estilo '{st}' recusado pela voz '{v}'; sem estilo" if st
                                             else f"voz '{v}' recusada pelo Azure; usando a padrão")
            raise VoiceError("O Azure Speech não devolveu áudio.")
        if self.backend == "command":
            return self._command(text)
        raise VoiceError("Fala das respostas não configurada (AZURE_SPEECH_KEY + AZURE_SPEECH_REGION, BYTE_VOICE_TTS_URL "
                         "ou BYTE_TTS_COMMAND).")

    def _http(self, text: str, voice: Optional[str] = None, speed: Optional[float] = None) -> Tuple[bytes, str]:
        endpoint = self.url if self.url.endswith("/audio/speech") else self.url + "/audio/speech"
        payload: Dict[str, Any] = {"model": self.model or "tts-1", "input": text, "voice": voice or self.voice or "alloy",
                                   "response_format": "opus"}
        if speed and abs(float(speed) - 1.0) > 0.01:
            payload["speed"] = float(speed)
        headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT}
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

    def azure_voice(self, voice: Optional[str]) -> str:
        """Nome de voz do Azure para o agente: aceita nomes do Azure, traduz os do Kokoro, senão usa a padrão."""
        for cand in (voice, self.voice):
            cand = (cand or "").strip()
            if _AZURE_VOICE_RE.match(cand):
                return cand
            if cand in KOKORO_TO_AZURE and cand == (voice or "").strip():
                return KOKORO_TO_AZURE[cand]
        return AZURE_DEFAULT_VOICE

    def _azure(self, text: str, voice: str, speed: Optional[float] = None, style: Optional[str] = None,
               style_degree: Optional[float] = None) -> Tuple[bytes, str]:
        lang = "-".join(voice.split("-")[:2])
        # v13.12: vozes "Multilingual" de outro idioma (ex.: zh-CN-XiaoxiaoMultilingualNeural) recebem a ordem de
        # falar em português do Brasil; sem isso, leriam o texto com o sotaque de origem.
        target = os.environ.get("BYTE_VOZ_IDIOMA", "pt-BR").strip() or "pt-BR"
        speak_lang = target if ("multilingual" in voice.lower() or lang.lower() == target.lower()) else lang
        body = _xml(text)
        if speak_lang.lower().startswith("pt"):
            body = apply_pronunciations(body)
        if speed and abs(float(speed) - 1.0) > 0.01:
            rate = max(-50, min(100, int(round((float(speed) - 1.0) * 100))))
            body = f'<prosody rate="{rate:+d}%">{body}</prosody>'
        if speak_lang.lower() != lang.lower():
            body = f'<lang xml:lang="{_xml(speak_lang)}">{body}</lang>'
        if style and re.fullmatch(r"[a-z-]{1,30}", style):
            degree = f' styledegree="{min(2.0, max(0.01, float(style_degree))):.2f}"' if style_degree else ""
            body = f'<mstts:express-as style="{style}"{degree}>{body}</mstts:express-as>'
        ssml = (f'<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" '
                f'xmlns:mstts="http://www.w3.org/2001/mstts" xml:lang="{lang}">'
                f'<voice name="{voice}">{body}</voice></speak>')
        last = ""
        for fmt, mime in (("ogg-24khz-16bit-mono-opus", "audio/ogg"), ("audio-24khz-48kbitrate-mono-mp3", "audio/mpeg")):
            req = urllib.request.Request(self.azure_endpoint, data=ssml.encode("utf-8"), method="POST", headers={
                "Ocp-Apim-Subscription-Key": self.azure_key, "Content-Type": "application/ssml+xml",
                "X-Microsoft-OutputFormat": fmt, "User-Agent": USER_AGENT})
            try:
                with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                    data = resp.read(MAX_AUDIO_BYTES + 1)
                if data and len(data) <= MAX_AUDIO_BYTES:
                    return data, mime
                last = "sem áudio"
            except urllib.error.HTTPError as err:
                if err.code == 400 and fmt.startswith("ogg"):
                    last = "HTTP 400"
                    continue
                raise VoiceError(f"O Azure Speech recusou o pedido (HTTP {err.code}).") from None
            except (urllib.error.URLError, OSError) as err:
                raise VoiceError(f"Azure Speech indisponível: {type(err).__name__}") from None
        raise VoiceError(f"O Azure Speech não devolveu áudio ({last}).")

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


def check_allowed(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Regras do agente ativo para comando de voz; lança erro se este canal não pode comandar por voz."""
    from . import engine
    policy = policy_for(engine.ACTIVE_AGENT)
    if not policy["ativo"]:
        raise VoiceError(f"O comando de voz está desligado para {policy['nome']}.")
    if policy["somente_dono"] and not payload.get("owner", False):
        raise PermissionError(f"{policy['nome']} só aceita comando de voz do dono (chat privado do Telegram, painel ou página de voz).")
    return policy


def handle_command(service: Any, payload: Dict[str, Any], kit: VoiceKit,
                   interact_kwargs: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Um comando de voz: texto já reconhecido (navegador/celular) ou áudio (Telegram, /voz) -> resposta do agente.

    payload: text | audio_b64 + mime; source (painel, telegram, api); owner (o canal confirmou que é o dono);
             hands_free (exige palavra de ativação); audio_reply (pede a resposta em áudio).
    """
    from . import engine
    policy = check_allowed(payload)
    came_as_audio = bool(payload.get("audio_b64")) or bool(payload.get("from_audio"))
    if payload.get("audio_b64"):
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
            data, mime = kit.synthesizer.synthesize(out["speech"], voice=policy.get("voz_servidor") or None,
                                                    speed=policy.get("velocidade"), style=policy.get("estilo") or None,
                                                    style_degree=policy.get("intensidade"))
            out["audio_b64"], out["audio_mime"] = base64.b64encode(data).decode("ascii"), mime
        except Exception as err:                            # sem áudio, a resposta em texto continua valendo
            out["audio_error"] = f"{type(err).__name__}: {str(err)[:160]}"
    return out

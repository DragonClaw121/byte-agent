"""Chat da Twitch por IRC sobre TLS (irc.chat.twitch.tv:6697), só com a biblioteca padrão.

Token: token de ACESSO DE USUÁRIO da conta do bot com os escopos chat:read e chat:edit.
Esses tokens expiram em poucas horas; informando client_id, client_secret e refresh_token a ponte
renova sozinha e guarda os novos tokens (o refresh token da Twitch é de uso único).
"""
from __future__ import annotations

import collections
import json
import os
import re
import socket
import ssl
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

MAX_CHAT_CHARS = 450                 # o limite da Twitch é 500 por mensagem
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]+")


def sanitize_outgoing(text: str, limit: int = MAX_CHAT_CHARS) -> str:
    """Torna um texto seguro para PRIVMSG:
    - remove quebras de linha e caracteres de controle (impede injetar comandos IRC crus);
    - remove '/', '.' e '!' do início (impede que a fala vire comando da Twitch, como /ban, ou de outros bots)."""
    clean = " ".join(_CONTROL_RE.sub(" ", str(text or "")).split())
    clean = clean.lstrip("/.! ​")
    return clean[:limit].rstrip()


def parse_irc_line(line: str) -> Tuple[Dict[str, str], str, str, List[str]]:
    """'@tags :prefixo COMANDO a b :resto' -> (tags, prefixo, comando, parâmetros)."""
    tags: Dict[str, str] = {}
    rest = line
    if rest.startswith("@"):
        raw_tags, _, rest = rest[1:].partition(" ")
        for item in raw_tags.split(";"):
            key, _, value = item.partition("=")
            tags[key] = (value.replace("\\s", " ").replace("\\:", ";").replace("\\r", "").replace("\\n", "").replace("\\\\", "\\"))
    prefix = ""
    if rest.startswith(":"):
        prefix, _, rest = rest[1:].partition(" ")
    head, sep, trailing = rest.partition(" :")
    parts = head.split()
    command = parts[0].upper() if parts else ""
    params = parts[1:] + ([trailing] if sep else [])
    return tags, prefix, command, params


@dataclass
class ChatMessage:
    user: str                         # login em minúsculas
    display_name: str
    text: str
    channel: str
    is_mod: bool = False
    is_broadcaster: bool = False
    is_subscriber: bool = False
    received_at: float = field(default_factory=time.monotonic)
    tags: Dict[str, str] = field(default_factory=dict)


class TwitchAuth:
    """Guarda o token de acesso e, se houver refresh_token + client_id + client_secret, renova quando expira."""

    def __init__(self, access_token: str, refresh_token: str = "", client_id: str = "", client_secret: str = "",
                 store_path: Optional[Path] = None, token_url: str = "https://id.twitch.tv/oauth2/token"):
        self.access_token = self._strip(access_token)
        self.refresh_token = str(refresh_token or "").strip()
        self.client_id = str(client_id or "").strip()
        self.client_secret = str(client_secret or "").strip()
        self.store_path = Path(store_path) if store_path else None
        self.token_url = token_url
        self.last_error: Optional[str] = None
        self._load_store()
        if not self.access_token and not self.can_refresh:
            raise ValueError("Informe o token da Twitch (BYTE_TWITCH_TOKEN) ou os dados para renovação.")

    @staticmethod
    def _strip(token: str) -> str:
        token = str(token or "").strip()
        return token[6:] if token.lower().startswith("oauth:") else token

    @property
    def can_refresh(self) -> bool:
        return bool(self.refresh_token and self.client_id and self.client_secret)

    def _load_store(self) -> None:
        """Tokens renovados em execuções anteriores valem mais que os do .env (o refresh token antigo já foi gasto)."""
        if not self.store_path or not self.store_path.exists():
            return
        try:
            saved = json.loads(self.store_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if saved.get("client_id") == self.client_id and saved.get("refresh_token"):
            self.access_token = self._strip(saved.get("access_token", "")) or self.access_token
            self.refresh_token = str(saved["refresh_token"])

    def _save_store(self) -> None:
        if not self.store_path:
            return
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.store_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"client_id": self.client_id, "access_token": self.access_token, "refresh_token": self.refresh_token}, handle)

    def refresh(self) -> bool:
        if not self.can_refresh:
            self.last_error = "renovação não configurada (faltam client_id, client_secret ou refresh_token)"
            return False
        body = urllib.parse.urlencode({"grant_type": "refresh_token", "refresh_token": self.refresh_token,
                                       "client_id": self.client_id, "client_secret": self.client_secret}).encode("ascii")
        try:
            req = urllib.request.Request(self.token_url, data=body, headers={"Content-Type": "application/x-www-form-urlencoded"})
            with urllib.request.urlopen(req, timeout=15.0) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            self.access_token = self._strip(data["access_token"])
            self.refresh_token = str(data.get("refresh_token") or self.refresh_token)
            self._save_store()
            self.last_error = None
            return True
        except Exception as err:      # nunca inclui o corpo da resposta: pode conter tokens
            self.last_error = f"{type(err).__name__} ao renovar o token"
            return False


class TwitchChat:
    def __init__(self, nick: str, auth: TwitchAuth, channel: str, on_message: Callable[[ChatMessage], None],
                 host: str = "irc.chat.twitch.tv", port: int = 6697, use_tls: bool = True,
                 messages_per_30s: int = 18):
        self.nick = str(nick or "").strip().lower()
        self.channel = str(channel or "").strip().lower().lstrip("#")
        if not re.fullmatch(r"[a-z0-9_]{3,25}", self.nick) or not re.fullmatch(r"[a-z0-9_]{3,25}", self.channel):
            raise ValueError("Nick e canal da Twitch devem ter 3–25 caracteres [a-z0-9_].")
        self.auth = auth
        self.on_message = on_message
        self.host, self.port, self.use_tls = host, int(port), bool(use_tls)
        self.messages_per_30s = int(messages_per_30s)
        self._sock: Optional[socket.socket] = None
        self._send_lock = threading.Lock()
        self._sent_times: Deque[float] = collections.deque()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.joined = threading.Event()
        self.fatal_error: Optional[str] = None
        self.last_error: Optional[str] = None
        self.sent_count = 0
        self.received_count = 0

    # ---------------- ciclo de vida ----------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="ByteTwitchChat")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._close()

    def _close(self) -> None:
        sock, self._sock = self._sock, None
        self.joined.clear()
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    def _run(self) -> None:
        backoff = 2.0
        while not self._stop.is_set() and not self.fatal_error:
            try:
                self._session()
                backoff = 2.0
            except Exception as err:
                self.last_error = f"{type(err).__name__}: {str(err)[:160]}"
            self._close()
            if self._stop.wait(backoff):
                break
            backoff = min(60.0, backoff * 2)

    def _raw(self, line: str) -> None:
        sock = self._sock
        if sock is None:
            raise OSError("sem conexão com a Twitch")
        with self._send_lock:
            sock.sendall((line + "\r\n").encode("utf-8"))

    def _session(self) -> None:
        sock = socket.create_connection((self.host, self.port), timeout=15.0)
        if self.use_tls:
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=self.host)
        sock.settimeout(1.0)
        self._sock = sock
        self._raw("CAP REQ :twitch.tv/tags twitch.tv/commands")
        self._raw(f"PASS oauth:{self.auth.access_token}")
        self._raw(f"NICK {self.nick}")
        buffer = b""
        last_activity = time.monotonic()
        while not self._stop.is_set():
            try:
                chunk = sock.recv(8192)
            except socket.timeout:
                if time.monotonic() - last_activity > 360.0:      # a Twitch envia PING a cada ~5 min
                    raise TimeoutError("sem atividade do servidor da Twitch")
                continue
            if not chunk:
                raise ConnectionError("a Twitch encerrou a conexão")
            last_activity = time.monotonic()
            buffer += chunk
            while b"\r\n" in buffer:
                raw_line, _, buffer = buffer.partition(b"\r\n")
                if self._handle_line(raw_line.decode("utf-8", errors="replace")):
                    return                                       # o servidor pediu reconexão

    def _handle_line(self, line: str) -> bool:
        tags, prefix, command, params = parse_irc_line(line)
        if command == "PING":
            self._raw("PONG :" + (params[-1] if params else "tmi.twitch.tv"))
        elif command == "001":
            self._raw(f"JOIN #{self.channel}")
        elif command in {"JOIN", "ROOMSTATE"}:
            self.joined.set()
            self.last_error = None
        elif command == "NOTICE" and params and any(
                marker in params[-1].lower() for marker in ("authentication failed", "improperly formatted auth")):
            if self.auth.refresh():
                return True                                      # reconecta com o token novo
            self.fatal_error = ("A Twitch recusou o token do bot e não foi possível renová-lo "
                                f"({self.auth.last_error}). Gere um novo token com chat:read e chat:edit.")
            return True
        elif command == "RECONNECT":
            return True
        elif command == "PRIVMSG" and len(params) >= 2:
            login = prefix.split("!", 1)[0].lower()
            self.received_count += 1
            badges = tags.get("badges", "")
            message = ChatMessage(
                user=login, display_name=tags.get("display-name") or login, text=params[-1],
                channel=params[0].lstrip("#"), is_mod=tags.get("mod") == "1" or "moderator/" in badges,
                is_broadcaster="broadcaster/" in badges, is_subscriber=tags.get("subscriber") == "1", tags=tags)
            try:
                self.on_message(message)
            except Exception as err:        # um erro no tratamento não pode derrubar a conexão
                self.last_error = f"on_message: {type(err).__name__}: {str(err)[:120]}"
        return False

    # ---------------- envio ----------------
    def send(self, text: str) -> bool:
        """Envia uma mensagem ao canal. Devolve False se estiver desconectado, vazio ou acima do limite de envio."""
        clean = sanitize_outgoing(text)
        if not clean or not self.joined.is_set():
            return False
        now = time.monotonic()
        while self._sent_times and now - self._sent_times[0] > 30.0:
            self._sent_times.popleft()
        if len(self._sent_times) >= self.messages_per_30s:
            return False
        try:
            self._raw(f"PRIVMSG #{self.channel} :{clean}")
        except OSError as err:
            self.last_error = f"{type(err).__name__}: {str(err)[:120]}"
            return False
        self._sent_times.append(now)
        self.sent_count += 1
        return True

    def status(self) -> Dict[str, Any]:
        return {"channel": self.channel, "nick": self.nick, "joined": self.joined.is_set(), "received": self.received_count,
                "sent": self.sent_count, "last_error": self.last_error, "fatal_error": self.fatal_error}

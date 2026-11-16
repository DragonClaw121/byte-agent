"""Cliente da API pública do VTube Studio (WebSocket, padrão ws://127.0.0.1:8001).

No VTube Studio: Configurações → "Start API (allow plugins)". Na primeira conexão o VTube Studio mostra
um aviso pedindo para autorizar o plugin "Byte Agent"; o token recebido fica salvo e é reutilizado.
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from .wsclient import WebSocketClient, WebSocketError


class VTSError(RuntimeError):
    pass


class VTubeStudioClient:
    API_NAME = "VTubeStudioPublicAPI"
    API_VERSION = "1.0"

    def __init__(self, url: str = "ws://127.0.0.1:8001", token_path: Optional[Path] = None,
                 plugin_name: str = "Byte Agent", plugin_developer: str = "Byte",
                 approval_timeout_s: float = 120.0, retry_cooldown_s: float = 10.0):
        if not (3 <= len(plugin_name) <= 32 and 3 <= len(plugin_developer) <= 32):
            raise ValueError("Nome do plugin e do desenvolvedor precisam ter de 3 a 32 caracteres.")
        self.url = url
        self.token_path = Path(token_path) if token_path else None
        self.plugin_name = plugin_name
        self.plugin_developer = plugin_developer
        self.approval_timeout_s = float(approval_timeout_s)
        self.retry_cooldown_s = float(retry_cooldown_s)
        self._ws: Optional[WebSocketClient] = None
        self._lock = threading.RLock()
        self._down_until = 0.0
        self._hotkeys: List[Dict[str, Any]] = []
        self.authenticated = False
        self.last_error: Optional[str] = None

    # ---------------- token ----------------
    def _load_token(self) -> str:
        try:
            return self.token_path.read_text(encoding="utf-8").strip() if self.token_path else ""
        except OSError:
            return ""

    def _save_token(self, token: str) -> None:
        if not self.token_path:
            return
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.token_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(token)

    # ---------------- transporte ----------------
    def _exchange(self, message_type: str, data: Optional[Dict[str, Any]], timeout_s: float) -> Dict[str, Any]:
        if self._ws is None:
            raise WebSocketError("sem conexão")
        request_id = uuid.uuid4().hex[:16]
        payload: Dict[str, Any] = {"apiName": self.API_NAME, "apiVersion": self.API_VERSION,
                                   "requestID": request_id, "messageType": message_type}
        if data is not None:
            payload["data"] = data
        self._ws.send_text(json.dumps(payload))
        deadline = time.monotonic() + timeout_s
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"VTube Studio não respondeu a {message_type}.")
            reply = json.loads(self._ws.recv_text(timeout_s=remaining))
            if reply.get("requestID") not in (request_id, None, ""):
                continue                                   # evento ou resposta antiga: ignora
            body = reply.get("data") if isinstance(reply.get("data"), dict) else {}
            if reply.get("messageType") == "APIError":
                raise VTSError(f"{message_type}: erro {body.get('errorID')} — {body.get('message')}")
            return body

    def connect(self) -> None:
        with self._lock:
            self.close()
            ws = WebSocketClient(self.url, timeout_s=5.0)
            ws.connect()
            self._ws = ws
            try:
                self._authenticate()
            except Exception:
                self.close()
                raise

    def _authenticate(self) -> None:
        ident = {"pluginName": self.plugin_name, "pluginDeveloper": self.plugin_developer}
        token = self._load_token()
        if token:
            res = self._exchange("AuthenticationRequest", dict(ident, authenticationToken=token), 5.0)
            if res.get("authenticated"):
                self.authenticated = True
                return
        # Sem token válido: o VTube Studio abre um aviso e o usuário precisa clicar em "Allow".
        res = self._exchange("AuthenticationTokenRequest", ident, self.approval_timeout_s)
        token = str(res.get("authenticationToken") or "")
        if not token:
            raise VTSError("O VTube Studio não entregou um token (o pedido foi negado?).")
        self._save_token(token)
        res = self._exchange("AuthenticationRequest", dict(ident, authenticationToken=token), 5.0)
        if not res.get("authenticated"):
            raise VTSError(f"Autenticação recusada: {res.get('reason')}")
        self.authenticated = True

    def close(self) -> None:
        with self._lock:
            ws, self._ws = self._ws, None
            self.authenticated = False
            if ws is not None:
                ws.close()

    def request(self, message_type: str, data: Optional[Dict[str, Any]] = None, timeout_s: float = 5.0) -> Dict[str, Any]:
        """Envia um pedido. Se a conexão caiu, reconecta uma vez; se o VTube Studio estiver fechado, falha
        rápido durante `retry_cooldown_s` para não travar quem chama (ex.: o laço de sincronia labial)."""
        with self._lock:
            if self._ws is None:
                if time.monotonic() < self._down_until:
                    raise VTSError(f"VTube Studio indisponível ({self.last_error})")
                try:
                    self.connect()
                except (OSError, WebSocketError, TimeoutError, ValueError) as err:
                    self._mark_down(err)
                    raise VTSError(f"Não consegui conectar ao VTube Studio em {self.url}: {err}") from err
            try:
                result = self._exchange(message_type, data, timeout_s)
                self.last_error = None
                return result
            except (OSError, WebSocketError, TimeoutError, ValueError) as err:
                self.close()
                self._mark_down(err)
                raise VTSError(f"Conexão com o VTube Studio perdida: {err}") from err

    def _mark_down(self, err: Exception) -> None:
        self.last_error = f"{type(err).__name__}: {str(err)[:120]}"
        self._down_until = time.monotonic() + self.retry_cooldown_s

    # ---------------- operações ----------------
    def hotkeys(self, refresh: bool = False) -> List[Dict[str, Any]]:
        if refresh or not self._hotkeys:
            res = self.request("HotkeysInCurrentModelRequest", {})
            self._hotkeys = list(res.get("availableHotkeys") or [])
        return self._hotkeys

    def trigger_hotkey(self, name_or_id: str) -> bool:
        """Dispara um hotkey pelo nome (como aparece no VTube Studio) ou pelo ID. False se não existir/falhar."""
        wanted = str(name_or_id or "").strip()
        if not wanted:
            return False
        try:
            target = wanted
            for attempt in (False, True):
                match = next((h for h in self.hotkeys(refresh=attempt)
                              if wanted.lower() in (str(h.get("name", "")).lower(), str(h.get("hotkeyID", "")).lower())), None)
                if match:
                    target = str(match.get("hotkeyID") or wanted)
                    break
            self.request("HotkeyTriggerRequest", {"hotkeyID": target})
            return True
        except VTSError as err:
            self.last_error = str(err)[:160]
            return False

    def inject(self, values: Dict[str, float], face_found: bool = False, mode: str = "set") -> None:
        """Injeta valores em parâmetros de entrada (ex.: {"MouthOpen": 0.7}). O VTube Studio devolve o controle
        ao rastreamento se o parâmetro ficar mais de 1 s sem ser reenviado."""
        self.request("InjectParameterDataRequest", {
            "faceFound": bool(face_found), "mode": mode,
            "parameterValues": [{"id": str(k), "value": float(v)} for k, v in values.items()],
        }, timeout_s=2.0)

    def status(self) -> Dict[str, Any]:
        return {"url": self.url, "connected": self._ws is not None, "authenticated": self.authenticated,
                "hotkeys": [h.get("name") for h in self._hotkeys], "last_error": self.last_error}

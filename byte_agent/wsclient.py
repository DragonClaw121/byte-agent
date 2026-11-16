"""Cliente WebSocket mínimo (RFC 6455) só com a biblioteca padrão.

Cobre o necessário para APIs locais em JSON como a do VTube Studio: handshake, quadros de texto
mascarados, fragmentação, ping/pong e fechamento. Não implementa extensões nem compressão.
"""
from __future__ import annotations

import base64
import hashlib
import os
import socket
import ssl
import struct
import threading
import urllib.parse
from typing import Optional

_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_MESSAGE_BYTES = 8 * 1024 * 1024


class WebSocketError(RuntimeError):
    pass


class WebSocketClosed(WebSocketError):
    pass


class WebSocketClient:
    def __init__(self, url: str, timeout_s: float = 10.0):
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in {"ws", "wss"} or not parsed.hostname:
            raise ValueError(f"URL WebSocket inválida: {url!r} (use ws://host:porta)")
        self.url = url
        self.secure = parsed.scheme == "wss"
        self.host = parsed.hostname
        self.port = parsed.port or (443 if self.secure else 80)
        self.path = (parsed.path or "/") + (("?" + parsed.query) if parsed.query else "")
        self.timeout_s = float(timeout_s)
        self._sock: Optional[socket.socket] = None
        self._buf = b""
        self._send_lock = threading.Lock()

    @property
    def connected(self) -> bool:
        return self._sock is not None

    def connect(self) -> None:
        self.close()
        sock = socket.create_connection((self.host, self.port), timeout=self.timeout_s)
        try:
            if self.secure:
                sock = ssl.create_default_context().wrap_socket(sock, server_hostname=self.host)
            key = base64.b64encode(os.urandom(16)).decode("ascii")
            host_header = self.host if self.port in (80, 443) else f"{self.host}:{self.port}"
            request = (
                f"GET {self.path} HTTP/1.1\r\nHost: {host_header}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
            )
            sock.sendall(request.encode("ascii"))
            raw = b""
            while b"\r\n\r\n" not in raw:
                chunk = sock.recv(4096)
                if not chunk:
                    raise WebSocketError("O servidor fechou a conexão durante o handshake.")
                raw += chunk
                if len(raw) > 16384:
                    raise WebSocketError("Cabeçalho de handshake grande demais.")
            head, _, rest = raw.partition(b"\r\n\r\n")
            lines = head.decode("latin-1").split("\r\n")
            if " 101" not in lines[0]:
                raise WebSocketError(f"Handshake recusado: {lines[0][:120]}")
            headers = {k.strip().lower(): v.strip() for k, _, v in (ln.partition(":") for ln in lines[1:])}
            expected = base64.b64encode(hashlib.sha1((key + _GUID).encode("ascii")).digest()).decode("ascii")
            if headers.get("sec-websocket-accept") != expected:
                raise WebSocketError("Sec-WebSocket-Accept inválido (o servidor não é um WebSocket?).")
            self._sock, self._buf = sock, rest
        except Exception:
            try:
                sock.close()
            except OSError:
                pass
            raise

    def close(self) -> None:
        sock, self._sock, self._buf = self._sock, None, b""
        if sock is not None:
            try:
                sock.sendall(self._frame(0x8, b""))
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass

    @staticmethod
    def _frame(opcode: int, payload: bytes) -> bytes:
        header = bytes([0x80 | opcode])
        n = len(payload)
        if n < 126:
            header += bytes([0x80 | n])
        elif n < 65536:
            header += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            header += bytes([0x80 | 127]) + struct.pack(">Q", n)
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload)) if n < 2048 else (
            int.from_bytes(payload, "big") ^ int.from_bytes((mask * (n // 4 + 1))[:n], "big")).to_bytes(n, "big")
        return header + mask + masked

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        sock = self._sock
        if sock is None:
            raise WebSocketClosed("WebSocket não conectado.")
        with self._send_lock:
            sock.sendall(self._frame(opcode, payload))

    def send_text(self, text: str) -> None:
        self._send_frame(0x1, text.encode("utf-8"))

    def _read_exact(self, n: int) -> bytes:
        sock = self._sock
        if sock is None:
            raise WebSocketClosed("WebSocket não conectado.")
        while len(self._buf) < n:
            chunk = sock.recv(max(4096, n - len(self._buf)))
            if not chunk:
                raise WebSocketClosed("Conexão encerrada pelo servidor.")
            self._buf += chunk
        data, self._buf = self._buf[:n], self._buf[n:]
        return data

    def recv_text(self, timeout_s: Optional[float] = None) -> str:
        """Devolve a próxima mensagem de texto. Lança TimeoutError se nada chegar em `timeout_s`."""
        sock = self._sock
        if sock is None:
            raise WebSocketClosed("WebSocket não conectado.")
        sock.settimeout(self.timeout_s if timeout_s is None else float(timeout_s))
        message = b""
        try:
            while True:
                b0, b1 = self._read_exact(2)
                fin, opcode = bool(b0 & 0x80), b0 & 0x0F
                length = b1 & 0x7F
                if length == 126:
                    length = struct.unpack(">H", self._read_exact(2))[0]
                elif length == 127:
                    length = struct.unpack(">Q", self._read_exact(8))[0]
                if length > MAX_MESSAGE_BYTES:
                    raise WebSocketError("Mensagem grande demais.")
                mask = self._read_exact(4) if (b1 & 0x80) else b""
                payload = self._read_exact(length)
                if mask:
                    payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
                if opcode == 0x8:
                    raise WebSocketClosed("O servidor enviou um quadro de fechamento.")
                if opcode == 0x9:
                    self._send_frame(0xA, payload)
                    continue
                if opcode == 0xA:
                    continue
                message += payload
                if opcode == 0x8:
                    raise WebSocketClosed("O servidor enviou um quadro de fechamento.")
                if opcode == 0x9:
                    self._send_frame(0xA, payload)
                    continue
                if opcode == 0xA:
                    continue
                if opcode not in (0x0, 0x1):
                    raise WebSocketError(f"Opcode de dados não suportado ou inesperado: {opcode}")
                if len(message) > MAX_MESSAGE_BYTES:
                    raise WebSocketError("Mensagem grande demais.")
                if fin:
                    return message.decode("utf-8", errors="replace")
        except socket.timeout as err:
            raise TimeoutError("Tempo esgotado aguardando resposta do WebSocket.") from err
        except WebSocketClosed:
            self.close()
            raise

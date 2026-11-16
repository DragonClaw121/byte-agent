"""Servidores simulados usados pelos testes: VTube Studio (WebSocket), Twitch (IRC) e um TTS de mentira."""
import base64
import hashlib
import json
import socket
import socketserver
import struct
import threading
import time

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

FAKE_TTS = r'''
import math, struct, sys, wave
text = sys.stdin.read()
out = sys.argv[1]
rate = 8000
frames = bytearray()
for i in range(int(rate * 0.4)):                      # 0,4 s: metade com som, metade em silêncio
    amp = 12000 if i < rate * 0.2 else 0
    frames += struct.pack("<h", int(amp * math.sin(i * 0.3)))
with wave.open(out, "wb") as w:
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate); w.writeframes(bytes(frames))
open(out + ".txt", "w", encoding="utf-8").write(text)
'''


class _Threaded(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


def _recv_exact(sock, n):
    data = b""
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:
            raise ConnectionError("fechado")
        data += chunk
    return data


def ws_read(sock):
    b0, b1 = _recv_exact(sock, 2)
    length = b1 & 0x7F
    if length == 126:
        length = struct.unpack(">H", _recv_exact(sock, 2))[0]
    elif length == 127:
        length = struct.unpack(">Q", _recv_exact(sock, 8))[0]
    mask = _recv_exact(sock, 4) if b1 & 0x80 else b""
    payload = _recv_exact(sock, length)
    if mask:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return b0 & 0x0F, payload, bool(b1 & 0x80)


def ws_frame(payload, opcode=0x1, fin=True):
    head = bytes([(0x80 if fin else 0) | opcode])
    n = len(payload)
    if n < 126:
        head += bytes([n])
    elif n < 65536:
        head += bytes([126]) + struct.pack(">H", n)
    else:
        head += bytes([127]) + struct.pack(">Q", n)
    return head + payload


class FakeVTS:
    """Subconjunto da API do VTube Studio: autenticação por token, hotkeys e injeção de parâmetros."""

    def __init__(self, approve=True, fragment=False, ping_first=False):
        self.approve, self.fragment, self.ping_first = approve, fragment, ping_first
        self.valid_tokens = set()
        self.requests = []
        self.triggered = []
        self.injected = []
        self.unmasked_frames = 0
        self.hotkeys = [{"name": "Happy", "hotkeyID": "id-happy", "type": "ToggleExpression"},
                        {"name": "Neutral", "hotkeyID": "id-neutral", "type": "RemoveAllExpressions"}]
        self.padding = ""
        outer = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                sock = self.request
                raw = b""
                while b"\r\n\r\n" not in raw:
                    chunk = sock.recv(4096)
                    if not chunk:
                        return
                    raw += chunk
                key = [ln.split(":", 1)[1].strip() for ln in raw.decode().split("\r\n") if ln.lower().startswith("sec-websocket-key")][0]
                accept = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
                sock.sendall(f"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: {accept}\r\n\r\n".encode())
                try:
                    while True:
                        opcode, payload, masked = ws_read(sock)
                        if not masked:
                            outer.unmasked_frames += 1
                        if opcode == 0x8:
                            return
                        if opcode != 0x1:
                            continue
                        req = json.loads(payload.decode())
                        outer.requests.append(req)
                        reply = json.dumps(outer.answer(req)).encode()
                        if outer.ping_first:
                            sock.sendall(ws_frame(b"hb", opcode=0x9))
                        if outer.fragment and len(reply) > 10:
                            sock.sendall(ws_frame(reply[:10], opcode=0x1, fin=False) + ws_frame(reply[10:], opcode=0x0, fin=True))
                        else:
                            sock.sendall(ws_frame(reply))
                except (ConnectionError, OSError):
                    return

        self.httpd = _Threaded(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"ws://127.0.0.1:{self.httpd.server_address[1]}"

    def answer(self, req):
        kind, data = req["messageType"], req.get("data") or {}
        base = {"apiName": "VTubeStudioPublicAPI", "apiVersion": "1.0", "requestID": req["requestID"]}

        def ok(message_type, body):
            return dict(base, messageType=message_type, data=body)

        def err(code, message):
            return dict(base, messageType="APIError", data={"errorID": code, "message": message})

        if kind == "AuthenticationTokenRequest":
            if not self.approve:
                return err(50, "User has denied API access for your plugin.")
            token = f"tok-{len(self.valid_tokens) + 1}"
            self.valid_tokens.add(token)
            return ok("AuthenticationTokenResponse", {"authenticationToken": token})
        if kind == "AuthenticationRequest":
            good = data.get("authenticationToken") in self.valid_tokens
            return ok("AuthenticationResponse", {"authenticated": good, "reason": "ok" if good else "Token invalid or revoked."})
        if kind == "HotkeysInCurrentModelRequest":
            return ok("HotkeysInCurrentModelResponse", {"modelLoaded": True, "availableHotkeys": self.hotkeys, "pad": self.padding})
        if kind == "HotkeyTriggerRequest":
            if data.get("hotkeyID") not in {h["hotkeyID"] for h in self.hotkeys}:
                return err(205, "Hotkey not found")
            self.triggered.append(data["hotkeyID"])
            return ok("HotkeyTriggerResponse", {"hotkeyID": data["hotkeyID"]})
        if kind == "InjectParameterDataRequest":
            self.injected.append(data)
            return ok("InjectParameterDataResponse", {})
        return err(1, "unknown request")

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class FakeTwitchIRC:
    """Servidor IRC em TCP puro que imita o login e o chat da Twitch."""

    def __init__(self, valid_tokens=("goodtoken",)):
        self.valid_tokens = set(valid_tokens)
        self.lines = []            # tudo o que o cliente enviou
        self.logins = []           # tokens apresentados em cada conexão
        self.clients = []
        self.lock = threading.Lock()
        outer = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                sock = self.request
                with outer.lock:
                    outer.clients.append(sock)
                buf = b""
                try:
                    while True:
                        chunk = sock.recv(4096)
                        if not chunk:
                            return
                        buf += chunk
                        while b"\r\n" in buf:
                            raw, _, buf = buf.partition(b"\r\n")
                            line = raw.decode("utf-8", "replace")
                            outer.lines.append(line)
                            if line.startswith("PASS "):
                                self.token = line.split("oauth:", 1)[-1]
                                outer.logins.append(self.token)
                            elif line.startswith("NICK "):
                                if getattr(self, "token", "") in outer.valid_tokens:
                                    sock.sendall(f":tmi.twitch.tv 001 {line[5:]} :Welcome, GLHF!\r\n".encode())
                                else:
                                    sock.sendall(b":tmi.twitch.tv NOTICE * :Login authentication failed\r\n")
                            elif line.startswith("JOIN "):
                                chan = line[5:]
                                sock.sendall(f":bot!bot@bot.tmi.twitch.tv JOIN {chan}\r\n@emote-only=0 :tmi.twitch.tv ROOMSTATE {chan}\r\n".encode())
                except OSError:
                    return

        self.httpd = _Threaded(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.port = self.httpd.server_address[1]

    def push(self, raw_line):
        with self.lock:
            for sock in list(self.clients):
                try:
                    sock.sendall((raw_line + "\r\n").encode("utf-8"))
                except OSError:
                    self.clients.remove(sock)

    def viewer_says(self, user, text, channel="canal", mod=False, broadcaster=False):
        badges = "broadcaster/1" if broadcaster else ("moderator/1" if mod else "")
        self.push(f"@badges={badges};display-name={user.capitalize()};mod={'1' if mod else '0'};subscriber=0 "
                  f":{user}!{user}@{user}.tmi.twitch.tv PRIVMSG #{channel} :{text}")

    def sent_privmsgs(self):
        return [ln for ln in self.lines if ln.startswith("PRIVMSG ")]

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def wait_until(predicate, timeout=5.0, step=0.02):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(step)
    return bool(predicate())

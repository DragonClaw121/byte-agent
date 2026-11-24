"""Kronus como aplicativo (PWA) — v13.16.

A página /voz vira um app instalável: ícone na tela inicial, tela cheia, abre mesmo sem rede (a casca fica em cache)
e, no Android, aparece na lista de apps (dá para abrir com "Ok Google, abrir Kronus").

Sem token gigante: no Telegram do agente, /parear gera um código de 6 dígitos (e um link de um toque), válido por
10 minutos e de uso único. O app troca o código por um TOKEN PRÓPRIO do aparelho: cada aparelho pode ser revogado
sozinho (/dispositivos, /desparear <id>). No banco fica só o hash do token.
"""
from __future__ import annotations

import collections
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import struct
import threading
import time
import zlib
from datetime import datetime, timezone
from typing import Any, Deque, Dict, List, Optional

CODE_TTL_S = 600.0
MAX_FAILURES = 10                 # tentativas erradas em 10 min: o pareamento trava por 10 min
MAX_DEVICES = 20

SCHEMA = """
CREATE TABLE IF NOT EXISTS api_devices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    last_used TEXT,
    revoked INTEGER NOT NULL DEFAULT 0
);
"""


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class DeviceRegistry:
    def __init__(self, db: Any, clock=time.time):
        self.db, self.clock = db, clock
        self._lock = threading.Lock()
        self._code: Optional[Dict[str, Any]] = None
        self._failures: Deque[float] = collections.deque()
        self._last_touch: Dict[int, float] = {}
        with db.connection() as conn:
            conn.executescript(SCHEMA)

    # ---------------- pareamento
    def new_code(self) -> Dict[str, Any]:
        """Um código por vez: gerar outro invalida o anterior."""
        with self._lock:
            code = f"{secrets.randbelow(10 ** 6):06d}"
            self._code = {"code": code, "expires": self.clock() + CODE_TTL_S}
            return {"code": code, "expires_in_min": int(CODE_TTL_S // 60)}

    def _locked(self, now: float) -> bool:
        while self._failures and now - self._failures[0] > CODE_TTL_S:
            self._failures.popleft()
        return len(self._failures) >= MAX_FAILURES

    def pair(self, code: str, name: str = "") -> str:
        now = self.clock()
        with self._lock:
            if self._locked(now):
                raise PermissionError("Muitas tentativas erradas. Espere 10 minutos e gere um código novo com /parear.")
            current = self._code
            ok = bool(current) and now <= current["expires"] and hmac.compare_digest(str(code or "").strip(), current["code"])
            if not ok:
                self._failures.append(now)
                raise PermissionError("Código inválido ou vencido. Mande /parear no Telegram para gerar outro.")
            self._code = None                                        # uso único
        with self.db.connection() as conn:
            active = conn.execute("SELECT COUNT(*) AS n FROM api_devices WHERE revoked = 0").fetchone()["n"]
            if active >= MAX_DEVICES:
                raise PermissionError(f"Limite de {MAX_DEVICES} aparelhos. Remova um com /desparear.")
            token = secrets.token_urlsafe(32)
            label = " ".join(re.sub(r"[^\w .,()/+-]", " ", str(name or "")).split())[:60] or "aparelho"
            conn.execute("INSERT INTO api_devices (name, token_hash, created_at) VALUES (?, ?, ?)", (label, _hash(token), _now_iso()))
        self.db.record_audit("device_paired", "app_movel", "success", {"name": label})
        return token

    # ---------------- uso
    def verify(self, token: str) -> Optional[Dict[str, Any]]:
        if not token or len(token) < 20:
            return None
        with self.db.connection() as conn:
            row = conn.execute("SELECT id, name FROM api_devices WHERE token_hash = ? AND revoked = 0", (_hash(token),)).fetchone()
            if row is None:
                return None
            now = self.clock()
            if now - self._last_touch.get(row["id"], 0.0) > 300:     # registra o último uso sem escrever a cada pedido
                self._last_touch[row["id"]] = now
                conn.execute("UPDATE api_devices SET last_used = ? WHERE id = ?", (_now_iso(), row["id"]))
        return dict(row)

    def list(self) -> List[Dict[str, Any]]:
        with self.db.connection() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT id, name, created_at, last_used FROM api_devices WHERE revoked = 0 ORDER BY id").fetchall()]

    def revoke(self, device_id: int) -> bool:
        with self.db.connection() as conn:
            cur = conn.execute("UPDATE api_devices SET revoked = 1 WHERE id = ? AND revoked = 0", (int(device_id),))
        return cur.rowcount > 0


# ---------------------------------------------------------------- arquivos do app
def manifest(name: str) -> str:
    return json.dumps({
        "name": name, "short_name": name[:12], "description": f"{name}: fale com seu agente em qualquer lugar.",
        "id": "/voz", "start_url": "/voz?app=1&ouvir=1", "scope": "/voz", "display": "standalone", "orientation": "portrait",
        "background_color": "#0b0f17", "theme_color": "#0b0f17", "lang": "pt-BR",
        "icons": [{"src": "/icone-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
                  {"src": "/icone-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"}],
        "shortcuts": [{"name": f"Falar com {name}", "short_name": "Falar", "url": "/voz?app=1&ouvir=1",
                       "icons": [{"src": "/icone-192.png", "sizes": "192x192"}]}],
    }, ensure_ascii=False)


SERVICE_WORKER = """// Kronus: casca do app em cache (abre mesmo sem rede); a API vai sempre à rede.
const CACHE = 'kronus-app-v1';
const SHELL = ['/voz', '/manifest.webmanifest', '/icone-192.png', '/icone-512.png'];
self.addEventListener('install', e => { e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting())); });
self.addEventListener('activate', e => { e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k))))
  .then(() => self.clients.claim())); });
self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET' || url.origin !== location.origin || url.pathname.startsWith('/api/')) return;
  if (url.pathname === '/voz') {
    e.respondWith(fetch(e.request).then(r => { const copy = r.clone(); caches.open(CACHE).then(c => c.put('/voz', copy)); return r; })
      .catch(() => caches.match('/voz')));
    return;
  }
  if (SHELL.includes(url.pathname)) e.respondWith(caches.match(url.pathname).then(r => r || fetch(e.request)));
});
"""


def _png(size: int) -> bytes:
    """Ícone desenhado sem bibliotecas: fundo escuro, círculo azul e um "K" branco (área segura para ícone 'maskable')."""
    bg, blue, blue2, white = (11, 15, 23), (37, 99, 235), (96, 165, 250), (255, 255, 255)
    c = size / 2.0
    r = size * 0.36
    s = size / 512.0
    stroke = 34 * s

    def seg_dist(px: float, py: float, ax: float, ay: float, bx: float, by: float) -> float:
        dx, dy = bx - ax, by - ay
        t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
        return math.hypot(px - (ax + t * dx), py - (ay + t * dy))

    bar = (206 * s, 166 * s, 206 * s, 346 * s)
    up = (222 * s, 268 * s, 318 * s, 166 * s)
    down = (240 * s, 250 * s, 322 * s, 346 * s)
    rows = []
    for y in range(size):
        row = bytearray([0])
        for x in range(size):
            px, py = x + 0.5, y + 0.5
            d = math.hypot(px - c, py - c)
            if d <= r:
                k = min(seg_dist(px, py, *bar), seg_dist(px, py, *up), seg_dist(px, py, *down))
                if k <= stroke / 2:
                    col = white
                else:
                    t = (py - (c - r)) / (2 * r)
                    col = tuple(int(blue2[i] * (1 - t) + blue[i] * t) for i in range(3))
            else:
                col = bg
            row += bytes(col)
        rows.append(bytes(row))
    raw = zlib.compress(b"".join(rows), 9)

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", raw) + chunk(b"IEND", b""))


_ICONS: Dict[int, bytes] = {}
_ICON_LOCK = threading.Lock()


def icon(size: int) -> bytes:
    with _ICON_LOCK:
        if size not in _ICONS:
            _ICONS[size] = _png(size)
        return _ICONS[size]


STATIC = {"/icone-192.png": 192, "/icone-512.png": 512, "/apple-touch-icon.png": 180}


def public_base() -> str:
    """Endereço público do app (para o link de pareamento): BYTE_ENDERECO_PUBLICO ou o do Azure Container Apps."""
    url = (os.environ.get("BYTE_ENDERECO_PUBLICO", "") or os.environ.get("BYTE_ENDERECO", "")).strip().rstrip("/")
    if url and not url.startswith("http"):
        url = "https://" + url
    if url.startswith("https://"):
        return url
    name, suffix = os.environ.get("CONTAINER_APP_NAME", ""), os.environ.get("CONTAINER_APP_ENV_DNS_SUFFIX", "")
    return f"https://{name}.{suffix}" if name and suffix else ""


def install(service: Any) -> DeviceRegistry:
    reg = DeviceRegistry(service.db)
    service.device_registry = reg

    def pair_code(payload: Dict[str, Any]) -> Dict[str, Any]:
        if not getattr(service, "api_enabled", False):
            raise PermissionError("Este agente não tem o app/API ligado (canais.api no manifesto). Use /parear no bot do Kronus.")
        out = reg.new_code()
        base = public_base()
        out["link"] = f"{base}/voz#parear={out['code']}" if base else ""
        out["text"] = (f"📱 Código para conectar o app: {out['code']} (vale {out['expires_in_min']} min, uma vez só).\n"
                       + (f"No celular, toque: {out['link']}\nDepois: menu do navegador → Instalar app / Adicionar à tela inicial."
                          if out["link"] else "Abra a página /voz do agente no celular e digite o código.")
                       + (f"\n💻 Para gravar reuniões no notebook: {base}/reuniao#parear={out['code']}" if base else ""))
        return out

    def voice_page_token(payload: Dict[str, Any]) -> Dict[str, Any]:
        """v13.44: o botão "Página de voz" do painel já entra conectado (vira um aparelho em /dispositivos)."""
        if not getattr(service, "api_enabled", False):
            raise PermissionError("A página de voz é do Kronus (agente com app/API ligado).")
        return {"token": reg.pair(reg.new_code()["code"], "Navegador (botão do painel)"), "url": "/voz"}

    service.extra_actions.update({
        "voice_page_token": voice_page_token,
        "device_pair_code": pair_code,
        "device_list": lambda payload: {"devices": reg.list()},
        "device_revoke": lambda payload: {"revoked": reg.revoke(int(payload.get("device_id") or 0))},
    })
    return reg

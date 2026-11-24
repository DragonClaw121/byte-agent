"""Login da Microsoft pelo navegador — v13.54.

O login por "código de dispositivo" (abrir microsoft.com/devicelogin e digitar um código) é o método que a Microsoft e
muitas empresas passaram a bloquear ("método de entrada desatualizado/arriscado"). Agora é o login normal:
o agente manda um link, você entra com a conta, aceita, e a Microsoft volta para https://<endereço do agente>/oauth/microsoft.

Como no Google (google_agenda.py): a volta pode cair em outro processo da nuvem, então o código passa por uma pasta
compartilhada (BYTE_DADOS_PERSISTENTES/oauth) e quem pediu conclui o login. Proteções: "state" aleatório de uso único,
PKCE (S256) e prazo de 10 minutos. Sem segredo de aplicativo: o endereço de volta é cadastrado no aplicativo como
"Aplicativo móvel e da área de trabalho" (cliente público) — scripts/microsoft-app.sh faz isso.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any, Callable, Dict, Tuple

AUTHORITY = "https://login.microsoftonline.com/common/oauth2/v2.0"
LOGIN_TTL_S = 600
ROUTE = "/oauth/microsoft"

PAGE = ("<!doctype html><html lang='pt-BR'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width'>"
        "<title>Microsoft</title></head><body style='font-family:system-ui;background:#0b0f17;color:#eef2f7;padding:32px'>"
        "<h2>{title}</h2><p>{text}</p></body></html>")

# erros comuns da Microsoft (AADSTS...) em português, com o que fazer
HINTS = (
    ("aadsts50011", "O endereço de volta do agente não está cadastrado no aplicativo da Microsoft. Rode "
                    "scripts/microsoft-app.sh no Cloud Shell (ele cadastra) e tente de novo."),
    ("aadsts65001", "A sua empresa exige que a TI aprove o aplicativo antes (consentimento do administrador)."),
    ("aadsts90094", "A sua empresa exige que a TI aprove o aplicativo antes (consentimento do administrador)."),
    ("aadsts90095", "A sua empresa exige que a TI aprove o aplicativo antes (consentimento do administrador)."),
    ("admin", "A sua empresa exige que a TI aprove o aplicativo antes (consentimento do administrador)."),
    ("aadsts700016", "O aplicativo (BYTE_MS_CLIENT_ID) não existe ou não aceita contas desta organização. Rode "
                     "scripts/microsoft-app.sh."),
    ("aadsts7000218", "O aplicativo não está como cliente público: rode scripts/microsoft-app.sh."),
    ("aadsts53003", "Uma regra de acesso condicional da sua empresa bloqueou o login (fale com a TI)."),
    ("access_denied", "O login foi cancelado ou as permissões não foram aceitas."),
)


def explain(raw: str) -> str:
    low = str(raw or "").lower()
    return next((hint for marker, hint in HINTS if marker in low), "")


def handoff_dir(data_dir: Path) -> Path:
    base = os.environ.get("BYTE_DADOS_PERSISTENTES", "").strip()
    return (Path(base) if base else Path(data_dir).expanduser()) / "oauth"


def redirect_uri(read_secret: Callable[[Tuple[str, ...]], str]) -> str:
    custom = str(read_secret(("BYTE_MS_REDIRECT",)) or "").strip()
    if custom.startswith("https://"):
        return custom
    from .app_movel import public_base
    base = public_base()
    return base + ROUTE if base else ""


def receive(folder: Path, state: str, code: str, error: str = "", clock: Callable[[], float] = time.time) -> bool:
    """Chamado pela rota pública (em qualquer processo): deixa o código para quem pediu."""
    if not re.fullmatch(r"[A-Za-z0-9_\-]{20,64}", state or ""):
        return False
    req = folder / f"ms-{state}.pedido"
    if not req.exists():
        return False
    try:
        if float(req.read_text() or 0) < clock():
            req.unlink(missing_ok=True)
            return False
    except (OSError, ValueError):
        return False
    tmp = folder / f".ms-{state}.tmp"
    tmp.write_text(json.dumps({"code": code, "error": error[:600]}))
    os.chmod(tmp, 0o600)
    tmp.replace(folder / f"ms-{state}.codigo")
    req.unlink(missing_ok=True)                                  # uso único
    return True


class BrowserLogin:
    """Um login (link) por vez por conta/assunto. on_tokens recebe o JSON de tokens da Microsoft."""

    def __init__(self, folder: Path, client_id: Callable[[], str], read_secret: Callable[[Tuple[str, ...]], str],
                 form: Callable[[str, Dict[str, str]], Tuple[int, Dict[str, Any]]], clock: Callable[[], float] = time.time):
        self.folder, self.client_id, self.read_secret, self.form, self.clock = folder, client_id, read_secret, form, clock
        self.pending: Dict[str, Dict[str, Any]] = {}
        self.background = True

    def available(self) -> bool:
        """Só depois que o endereço de volta foi cadastrado no aplicativo (scripts/microsoft-app.sh grava
        BYTE_MS_LOGIN=navegador); senão a Microsoft recusaria o link e fica o login por código."""
        mode = str(self.read_secret(("BYTE_MS_LOGIN",)) or "").strip().lower()
        custom = str(self.read_secret(("BYTE_MS_REDIRECT",)) or "").strip()
        return (mode == "navegador" or custom.startswith("https://")) and bool(redirect_uri(self.read_secret))

    def start(self, scopes: str, login_hint: str, on_tokens: Callable[[Dict[str, Any]], Any],
              on_error: Callable[[str], None]) -> Dict[str, Any]:
        client = self.client_id()
        uri = redirect_uri(self.read_secret)
        state = secrets.token_urlsafe(24)
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        self.folder.mkdir(parents=True, exist_ok=True)
        (self.folder / f"ms-{state}.pedido").write_text(str(int(self.clock() + LOGIN_TTL_S)))
        self.pending[state] = {"verifier": verifier, "scopes": scopes, "uri": uri, "client": client,
                               "expires": self.clock() + LOGIN_TTL_S, "on_tokens": on_tokens, "on_error": on_error}
        params = {"client_id": client, "response_type": "code", "redirect_uri": uri, "response_mode": "query",
                  "scope": scopes, "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
                  "prompt": "select_account"}
        if login_hint:
            params["login_hint"] = login_hint
        url = f"{AUTHORITY}/authorize?" + urllib.parse.urlencode(params)
        if self.background:
            threading.Thread(target=self.wait, args=(state,), daemon=True, name="ByteLoginMicrosoft").start()
        return {"url": url, "state": state}

    def wait(self, state: str, sleep: Callable[[float], None] = time.sleep, step: float = 2.0) -> bool:
        info = self.pending.get(state)
        while info and self.clock() < info["expires"]:
            path = self.folder / f"ms-{state}.codigo"
            if path.exists():
                data = json.loads(path.read_text() or "{}")
                path.unlink(missing_ok=True)
                self.pending.pop(state, None)
                if data.get("error") or not data.get("code"):
                    raw = str(data.get("error") or "sem código")
                    info["on_error"](explain(raw) or raw[:240])
                    return False
                return self.exchange(info, str(data["code"]))
            sleep(step)
        self.pending.pop(state, None)
        (self.folder / f"ms-{state}.pedido").unlink(missing_ok=True)
        if info:
            info["on_error"]("O link de login venceu (10 minutos). Peça outro.")
        return False

    def exchange(self, info: Dict[str, Any], code: str) -> bool:
        form = {"client_id": info["client"], "grant_type": "authorization_code", "code": code,
                "redirect_uri": info["uri"], "code_verifier": info["verifier"], "scope": info["scopes"]}
        secret = str(self.read_secret(("BYTE_MS_CLIENT_SECRET",)) or "")
        if secret:                                                # aplicativo cadastrado como "Web" (com segredo)
            form["client_secret"] = secret
        status, data = self.form(f"{AUTHORITY}/token", form)
        if status != 200 or not data.get("refresh_token"):
            raw = str(data.get("error_description") or data.get("error") or status)
            info["on_error"](explain(raw) or raw[:240])
            return False
        try:
            info["on_tokens"](data)
        except Exception as err:
            info["on_error"](f"{type(err).__name__}: {str(err)[:160]}")
            return False
        return True


def register_route(service: Any, folder: Path) -> None:
    """A rota pública /oauth/microsoft (a mesma para e-mail, agenda e arquivos)."""
    def route(h: Any, method: str, path: str, query: str) -> None:
        q = {k: v[0] for k, v in urllib.parse.parse_qs(query).items()}
        error = str(q.get("error") or "")
        detail = (error + ": " + str(q.get("error_description") or "")) if error else ""
        ok = method == "GET" and receive(folder, str(q.get("state") or ""), str(q.get("code") or ""), detail)
        good = ok and not error
        text = ("Pode fechar esta página: o agente avisa quando terminar." if good else
                (explain(detail) or "A Microsoft respondeu: " + detail[:200]) if error else
                "Este link de login venceu ou já foi usado. Peça outro.")
        import html as html_lib
        h._send_html(PAGE.format(title="✅ Pronto" if good else "⚠️ Não deu certo", text=html_lib.escape(text)))
    service.public_routes = dict(getattr(service, "public_routes", None) or {}, **{ROUTE: route})


def tokens_email(data: Dict[str, Any]) -> str:
    """O e-mail da conta, de dentro do id_token (quando a Microsoft manda um)."""
    try:
        payload = str(data.get("id_token") or "").split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        return str(claims.get("preferred_username") or claims.get("email") or "")
    except Exception:
        return ""


def parse_form_reply(body: bytes) -> Dict[str, Any]:
    try:
        return json.loads(body.decode("utf-8", "replace") or "{}")
    except ValueError:
        return {}


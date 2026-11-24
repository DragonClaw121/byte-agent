"""Google Agenda e Google Drive — v13.39 (R2.3).

  Google Agenda  entra na agenda unificada (resumo, "compromisso chegando", "hora de sair", horários livres) e nas
                 ferramentas comuns de agenda: ver, criar (com link do Meet), mover e cancelar — com aprovação.
  Google Drive   atas, planilhas, documentos e apresentações vão sozinhos para a pasta "Byte" do Drive
                 (/google drive on|off); a ferramenta drive_salvar manda um arquivo de entregas/.

Login: o Google NÃO aceita o login por código (de TV) para agenda e Drive, então é o login normal pelo navegador:
/google entrar manda um link; você entra, aceita, e o Google volta para https://<endereço do agente>/oauth/google.
Esse endereço precisa estar cadastrado no seu projeto do Google Cloud (scripts/google-conta.sh mostra qual é).
Na nuvem, a volta do Google cai num processo (o do Kronus) e quem pediu pode ser outro (o Bit): o código passa por
uma pasta compartilhada (BYTE_DADOS_PERSISTENTES/oauth) e quem pediu conclui o login. Proteções: "state" aleatório
de uso único, PKCE e prazo de 10 minutos. O token de renovação fica criptografado (BYTE_CHAVE_TOKENS).

Escopos: openid/email, calendar.events (só compromissos) e drive.file (só os arquivos que o próprio agente criar).
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
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN = "https://oauth2.googleapis.com/token"
CAL = "https://www.googleapis.com/calendar/v3"
DRIVE = "https://www.googleapis.com/drive/v3"
UPLOAD = "https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart&fields=id,name,webViewLink"
SCOPES = "openid email https://www.googleapis.com/auth/calendar.events https://www.googleapis.com/auth/drive.file"
SETTING = "google.contas"
FOLDER = "Byte"
LOGIN_TTL_S = 600


def _http(method: str, url: str, data: Optional[bytes] = None, headers: Optional[Dict[str, str]] = None,
          timeout: float = 30.0) -> Tuple[int, bytes]:
    req = urllib.request.Request(url, data=data, method=method, headers=dict({"User-Agent": "byte-agent/1.0"}, **(headers or {})))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(20 * 1024 * 1024)
    except urllib.error.HTTPError as err:
        return err.code, err.read(64 * 1024)


def handoff_dir(data_dir: Path) -> Path:
    """Pasta compartilhada entre os processos (na nuvem, o armazenamento persistente)."""
    base = os.environ.get("BYTE_DADOS_PERSISTENTES", "").strip()
    return (Path(base) if base else Path(data_dir).expanduser()) / "oauth"


def public_base() -> str:
    from .app_movel import public_base as base
    return base()


def _jwt_email(id_token: str) -> str:
    try:
        payload = id_token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return str(json.loads(base64.urlsafe_b64decode(payload)).get("email") or "")
    except Exception:
        return ""


class Google:
    def __init__(self, service: Any, vault: Any, read_secret: Callable[[Tuple[str, ...]], str], data_dir: Path,
                 http: Callable[..., Tuple[int, bytes]] = _http, clock: Callable[[], float] = time.time):
        self.service, self.db, self.vault, self.read_secret = service, service.db, vault, read_secret
        self.dir, self.http, self.clock = handoff_dir(data_dir), http, clock
        self._tokens: Dict[str, Tuple[str, float]] = {}
        self.pending: Dict[str, Dict[str, Any]] = {}            # state -> {verifier, expires}
        self.background_login = True
        self.last_error: Optional[str] = None
        self.notify: Callable[[str], None] = lambda text: self.db.enqueue_proactive_message("google", text, brain_used="GOOGLE")

    # ---------------- configuração e contas
    def client(self) -> Tuple[str, str]:
        cid, secret = self.read_secret(("BYTE_GOOGLE_CLIENT_ID",)), self.read_secret(("BYTE_GOOGLE_CLIENT_SECRET",))
        if not (cid and secret):
            raise PermissionError("Falta o projeto do Google (BYTE_GOOGLE_CLIENT_ID e o segredo). Siga docs/google.md e rode "
                                  "scripts/google-conta.sh.")
        return cid, secret

    def redirect_uri(self) -> str:
        base = str(self.read_secret(("BYTE_GOOGLE_REDIRECT",)) or "").strip() or (public_base() + "/oauth/google" if public_base() else "")
        if not base:
            raise PermissionError("Não sei o endereço público do agente para o Google voltar (BYTE_ENDERECO_PUBLICO).")
        return base

    def accounts(self) -> List[Dict[str, Any]]:
        return [a for a in (self.db.get_setting(SETTING, []) or []) if isinstance(a, dict) and a.get("email")]

    def ready_accounts(self) -> List[Dict[str, Any]]:
        return [a for a in self.accounts() if self.vault.get(f"google:{a['email'].lower()}")]

    def account(self, wanted: str = "") -> Dict[str, Any]:
        accs = self.ready_accounts()
        if not accs:
            raise PermissionError("Nenhuma conta Google conectada: mande /google entrar.")
        for a in accs:
            if wanted and wanted.lower() == a["email"].lower():
                return a
        return accs[0]

    # ---------------- login pelo navegador (com volta para /oauth/google)
    def start_login(self) -> Dict[str, Any]:
        cid, _ = self.client()
        state = secrets.token_urlsafe(24)
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        self.pending[state] = {"verifier": verifier, "expires": self.clock() + LOGIN_TTL_S}
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / f"{state}.pedido").write_text(str(int(self.clock() + LOGIN_TTL_S)))   # quem recebe a volta confere
        url = AUTH + "?" + urllib.parse.urlencode({
            "client_id": cid, "redirect_uri": self.redirect_uri(), "response_type": "code", "scope": SCOPES,
            "access_type": "offline", "prompt": "consent", "state": state, "code_challenge": challenge,
            "code_challenge_method": "S256", "include_granted_scopes": "true"})
        if self.background_login:
            threading.Thread(target=self.wait_login, args=(state,), daemon=True, name="ByteLoginGoogle").start()
        return {"text": "🔑 Login do Google (agenda e Drive)\nAbra este link, entre com a sua conta e aceite:\n" + url +
                        "\nSe aparecer \"O Google não verificou este app\": Avançado → Acessar (é o seu próprio projeto).\n"
                        "O link vale 10 minutos. Eu aviso aqui quando terminar.\n"
                        "Se der \"redirect_uri_mismatch\": /google endereco mostra o que cadastrar no Google Cloud.",
                "url": url, "state": state}

    def redirect_help(self) -> str:
        """v13.55: o erro 400 redirect_uri_mismatch = o endereço de volta não está IDÊNTICO no cliente OAuth."""
        try:
            uri = self.redirect_uri()
        except PermissionError as err:
            return f"⚠️ {err}"
        cid = self.read_secret(("BYTE_GOOGLE_CLIENT_ID",)) or "(BYTE_GOOGLE_CLIENT_ID não configurado)"
        return ("🔗 Endereço de volta do login do Google (o agente manda exatamente este):\n"
                f"{uri}\n\n"
                "No Google Cloud → Google Auth Platform → Clientes → abra o cliente com o ID\n"
                f"{cid}\n"
                "e confira em \"URIs de redirecionamento autorizados\" (NÃO em \"Origens JavaScript\"):\n"
                "• o endereço colado igualzinho: https, sem barra no fim, sem espaço;\n"
                "• o cliente é do tipo \"Aplicativo da Web\" e é o mesmo ID acima (se criou outro cliente, rode de novo "
                "google-conta.sh com o ID e a chave dele).\n"
                "Depois de salvar, o Google pode levar alguns minutos para aceitar. Então: /google entrar.")

    def receive(self, state: str, code: str, error: str = "") -> bool:
        """Chamado pela rota pública /oauth/google (em qualquer processo): deixa o código para quem pediu."""
        if not re.fullmatch(r"[A-Za-z0-9_\-]{20,64}", state or ""):
            return False
        req = self.dir / f"{state}.pedido"
        if not req.exists():
            return False
        try:
            if float(req.read_text() or 0) < self.clock():
                req.unlink(missing_ok=True)
                return False
        except (OSError, ValueError):
            return False
        tmp = self.dir / f".{state}.tmp"
        tmp.write_text(json.dumps({"code": code, "error": error}))
        os.chmod(tmp, 0o600)
        tmp.replace(self.dir / f"{state}.codigo")
        req.unlink(missing_ok=True)                               # uso único
        return True

    def wait_login(self, state: str, sleep: Callable[[float], None] = time.sleep, step: float = 2.0) -> bool:
        info = self.pending.get(state)
        while info and self.clock() < info["expires"]:
            path = self.dir / f"{state}.codigo"
            if path.exists():
                data = json.loads(path.read_text() or "{}")
                path.unlink(missing_ok=True)
                self.pending.pop(state, None)
                if data.get("error") or not data.get("code"):
                    self.notify("⚠️ Login do Google não concluído: " + str(data.get("error") or "sem código")[:120])
                    return False
                return self._exchange(data["code"], info["verifier"])
            sleep(step)
        self.pending.pop(state, None)
        (self.dir / f"{state}.pedido").unlink(missing_ok=True)
        return False

    def _form(self, form: Dict[str, str]) -> Tuple[int, Dict[str, Any]]:
        code, body = self.http("POST", TOKEN, urllib.parse.urlencode(form).encode(), {"Content-Type": "application/x-www-form-urlencoded"})
        try:
            return code, json.loads(body.decode("utf-8", "replace") or "{}")
        except ValueError:
            return code, {}

    def _exchange(self, code: str, verifier: str) -> bool:
        cid, secret = self.client()
        status, data = self._form({"code": code, "client_id": cid, "client_secret": secret, "redirect_uri": self.redirect_uri(),
                                   "grant_type": "authorization_code", "code_verifier": verifier})
        if status != 200 or not data.get("refresh_token"):
            self.notify("⚠️ O Google recusou o login: " + str(data.get("error_description") or data.get("error") or status)[:160])
            return False
        email = _jwt_email(str(data.get("id_token") or "")).strip()
        if not email:
            self.notify("⚠️ Login do Google feito, mas não consegui ler o e-mail da conta.")
            return False
        accs = [a for a in self.accounts() if a["email"].lower() != email.lower()] + [{"email": email}]
        self.db.set_setting(SETTING, accs)
        self.vault.put(f"google:{email.lower()}", data["refresh_token"])
        self._tokens[email.lower()] = (data.get("access_token", ""), self.clock() + float(data.get("expires_in") or 3600))
        self.db.record_audit("google_login", "google", "success", {})
        self.notify(f"✅ Google conectado ({email}): agenda e Drive. Veja /compromissos.")
        return True

    def token(self, acc: Dict[str, Any]) -> str:
        key = acc["email"].lower()
        cached = self._tokens.get(key)
        if cached and cached[0] and cached[1] - 120 > self.clock():
            return cached[0]
        refresh = self.vault.get(f"google:{key}")
        if not refresh:
            raise PermissionError("A conta Google precisa de login de novo: /google entrar")
        cid, secret = self.client()
        status, data = self._form({"client_id": cid, "client_secret": secret, "refresh_token": refresh, "grant_type": "refresh_token"})
        if status != 200 or not data.get("access_token"):
            if data.get("error") == "invalid_grant":
                self.vault.drop(f"google:{key}")
                raise PermissionError("O login do Google expirou ou foi revogado: /google entrar")
            raise PermissionError(f"O Google recusou a renovação: {str(data.get('error_description') or status)[:160]}")
        self._tokens[key] = (data["access_token"], self.clock() + float(data.get("expires_in") or 3600))
        return data["access_token"]

    def call(self, acc: Dict[str, Any], method: str, url: str, body: Any = None, raw: Optional[bytes] = None,
             headers: Optional[Dict[str, str]] = None) -> Any:
        h = {"Authorization": f"Bearer {self.token(acc)}"}
        h.update(headers or {})
        data = raw
        if body is not None:
            data, h["Content-Type"] = json.dumps(body).encode("utf-8"), "application/json"
        code, out = self.http(method, url, data, h)
        if code >= 400:
            try:
                msg = json.loads(out.decode("utf-8", "replace")).get("error", {}).get("message", "")
            except (ValueError, AttributeError):
                msg = out[:160].decode("utf-8", "replace")
            self.last_error = f"HTTP {code}: {msg[:160]}"
            raise PermissionError(f"O Google recusou ({self.last_error})")
        self.last_error = None
        return json.loads(out.decode("utf-8", "replace")) if out else {}

    # ---------------- agenda
    def events(self, start: datetime, end: datetime) -> List[Dict[str, Any]]:
        from . import gatilhos
        out = []
        for acc in self.ready_accounts():
            q = urllib.parse.urlencode({"timeMin": start.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                                        "timeMax": end.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                                        "singleEvents": "true", "orderBy": "startTime", "maxResults": "100"})
            data = self.call(acc, "GET", f"{CAL}/calendars/primary/events?{q}")
            for ev in data.get("items") or []:
                if ev.get("status") == "cancelled":
                    continue
                st, en = ev.get("start") or {}, ev.get("end") or {}
                if st.get("date"):
                    tz = gatilhos._tz()
                    when = datetime.strptime(st["date"], "%Y-%m-%d").replace(tzinfo=tz) if tz else datetime.strptime(st["date"], "%Y-%m-%d").astimezone()
                    all_day, finish = True, None
                else:
                    when, all_day = datetime.fromisoformat(st["dateTime"].replace("Z", "+00:00")), False
                    finish = datetime.fromisoformat(en["dateTime"].replace("Z", "+00:00")) if en.get("dateTime") else None
                meet = ev.get("hangoutLink") or next((p.get("uri") for p in (ev.get("conferenceData") or {}).get("entryPoints") or []
                                                      if p.get("entryPointType") == "video"), "")
                out.append({"when": when, "end": finish, "all_day": all_day, "title": str(ev.get("summary") or "compromisso")[:160],
                            "location": str(ev.get("location") or "")[:120], "uid": f"g:{str(ev.get('id'))[-24:]}",
                            "id": ev.get("id"), "account": acc["email"], "source": "google", "online_url": meet or ""})
        return out

    def create_event(self, title: str, start: datetime, end: datetime, location: str = "", attendees: Optional[List[str]] = None,
                     online: bool = False, account: str = "") -> Dict[str, Any]:
        acc = self.account(account)
        body: Dict[str, Any] = {"summary": title[:200], "start": {"dateTime": start.isoformat()}, "end": {"dateTime": end.isoformat()}}
        if location:
            body["location"] = location[:200]
        if attendees:
            body["attendees"] = [{"email": a} for a in attendees[:20]]
        url = f"{CAL}/calendars/primary/events?sendUpdates={'all' if attendees else 'none'}"
        if online:
            body["conferenceData"] = {"createRequest": {"requestId": uuid.uuid4().hex, "conferenceSolutionKey": {"type": "hangoutsMeet"}}}
            url += "&conferenceDataVersion=1"
        res = self.call(acc, "POST", url, body)
        self.db.record_audit("google_event_created", "google", "success", {"attendees": len(attendees or [])})
        return {"id": res.get("id"), "link": res.get("htmlLink"), "online_url": res.get("hangoutLink")}

    def move_event_id(self, account: str, eid: str, start: datetime, end: datetime) -> Dict[str, Any]:
        self.call(self.account(account), "PATCH", f"{CAL}/calendars/primary/events/{urllib.parse.quote(eid)}?sendUpdates=all",
                  {"start": {"dateTime": start.isoformat()}, "end": {"dateTime": end.isoformat()}})
        return {"moved": True}

    def cancel_event_id(self, account: str, eid: str) -> Dict[str, Any]:
        self.call(self.account(account), "DELETE", f"{CAL}/calendars/primary/events/{urllib.parse.quote(eid)}?sendUpdates=all")
        return {"cancelled": True}

    # ---------------- Drive (só arquivos criados pelo próprio agente: escopo drive.file)
    def _folder(self, acc: Dict[str, Any], name: str = FOLDER) -> str:
        key = f"google.pasta.{acc['email'].lower()}.{name}"
        fid = self.db.get_setting(key)
        if fid:
            return str(fid)
        q = urllib.parse.urlencode({"q": f"name = '{name}' and mimeType = 'application/vnd.google-apps.folder' and trashed = false",
                                    "fields": "files(id)"})
        found = (self.call(acc, "GET", f"{DRIVE}/files?{q}").get("files") or [])
        fid = found[0]["id"] if found else self.call(acc, "POST", f"{DRIVE}/files?fields=id",
                                                     {"name": name, "mimeType": "application/vnd.google-apps.folder"})["id"]
        self.db.set_setting(key, fid)
        return fid

    def upload(self, path: Path, folder: str = FOLDER, account: str = "") -> Dict[str, Any]:
        acc = self.account(account)
        data = Path(path).read_bytes()
        if len(data) > 100 * 1024 * 1024:
            raise ValueError("Arquivo acima de 100 MB.")
        boundary = "byte" + uuid.uuid4().hex
        meta = json.dumps({"name": Path(path).name, "parents": [self._folder(acc, folder)]}).encode()
        body = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n".encode() + meta +
                f"\r\n--{boundary}\r\nContent-Type: application/octet-stream\r\n\r\n".encode() + data + f"\r\n--{boundary}--\r\n".encode())
        res = self.call(acc, "POST", UPLOAD, raw=body, headers={"Content-Type": f"multipart/related; boundary={boundary}"})
        self.db.record_audit("google_upload", "google", "success", {"file": Path(path).name})
        return {"name": res.get("name"), "link": res.get("webViewLink")}

    def auto_upload_on(self) -> bool:
        return bool(self.db.get_setting("google.drive_auto", True)) and bool(self.ready_accounts())

    def status(self) -> Dict[str, Any]:
        return {"accounts": [{"email": a["email"], "ready": bool(self.vault.get(f"google:{a['email'].lower()}"))} for a in self.accounts()],
                "drive_auto": bool(self.db.get_setting("google.drive_auto", True)), "last_error": self.last_error,
                "configured": bool(self.read_secret(("BYTE_GOOGLE_CLIENT_ID",)))}


def format_status(st: Dict[str, Any]) -> str:
    if not st["configured"]:
        return "Google ainda não configurado: siga docs/google.md (projeto no Google Cloud + scripts/google-conta.sh)."
    if not st["accounts"]:
        return "Nenhuma conta Google conectada. Mande /google entrar."
    lines = ["🟩 Google:"] + [f"• {a['email']} — {'✅ agenda e Drive' if a['ready'] else '🔑 login expirado: /google entrar'}"
                              for a in st["accounts"]]
    lines.append(f"Drive automático (atas, planilhas, documentos na pasta \"{FOLDER}\"): {'ligado' if st['drive_auto'] else 'desligado'}")
    if st.get("last_error"):
        lines.append(f"⚠️ {st['last_error']}")
    lines.append("Comandos: /compromissos · /google drive on|off · /google entrar · /google sair")
    return "\n".join(lines)


PAGE = ("<!doctype html><html lang='pt-BR'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width'>"
        "<title>Google</title></head><body style='font-family:system-ui;background:#0b0f17;color:#eef2f7;padding:32px'>"
        "<h2>{title}</h2><p>{text}</p></body></html>")


def install(service: Any, data_dir: Path) -> Google:
    from . import calendarios, engine as _engine
    from .caixas import TokenVault
    from .relogio import cached
    hub = getattr(service, "mail_hub", None)
    vault = getattr(getattr(service, "microsoft", None), "vault", None) or (hub.vault if hub is not None else
                                                                            TokenVault(Path(data_dir).expanduser() / "segredos" / "tokens-email.bin",
                                                                                       _engine._read_secret(("BYTE_CHAVE_TOKENS",))))
    g = Google(service, vault, _engine._read_secret, data_dir)
    service.google = g

    def provider(start: datetime, end: datetime) -> List[Dict[str, Any]]:
        if not g.ready_accounts():
            return []
        return cached(f"google-cal:{int(start.timestamp()) // 60}:{int(end.timestamp()) // 60}", 180.0, lambda: g.events(start, end))
    provider.active = lambda: bool(g.ready_accounts())        # type: ignore[attr-defined]
    service.calendar_providers = list(getattr(service, "calendar_providers", []) or []) + [provider]

    class Writer:
        ready = staticmethod(lambda: bool(g.ready_accounts()))
        create = staticmethod(lambda title, start, end, location, attendees, online: g.create_event(title, start, end, location, attendees, online))
        move = staticmethod(lambda account, eid, start, end: g.move_event_id(account, eid, start, end))
        cancel = staticmethod(lambda account, eid: g.cancel_event_id(account, eid))
    service.calendar_writers = dict(getattr(service, "calendar_writers", {}) or {}, google=Writer())
    calendarios.install_tools(service)

    def on_output(path: Path) -> None:
        if g.auto_upload_on():
            try:
                g.upload(path)
            except Exception as err:
                g.last_error = f"Drive: {type(err).__name__}: {str(err)[:120]}"
    service.output_hooks = list(getattr(service, "output_hooks", []) or []) + [on_output]

    def t_upload(args: Dict[str, Any], turn: Dict[str, Any]) -> str:
        provider_file = getattr(service, "file_provider", None)
        path = provider_file(str(args.get("arquivo") or "")) if callable(provider_file) else None
        if path is None:
            return "ERRO: arquivo não encontrado em entregas/ (use o nome exato de um arquivo gerado)."
        try:
            res = g.upload(path, str(args.get("pasta") or FOLDER))
        except (ValueError, PermissionError) as err:
            return f"ERRO: {err}"
        return json.dumps({"salvo": True, "link": res.get("link")}, ensure_ascii=False)

    service.extra_tools["drive_salvar"] = {
        "auto": True, "mission": True, "handler": t_upload, "intent": re.compile(r"\b(google drive|drive|nuvem|salv\w+ (?:no|na))\b"),
        "description": "Salva no Google Drive (pasta Byte) um arquivo gerado em entregas/.",
        "parameters": {"type": "object", "required": ["arquivo"], "properties": {"arquivo": {"type": "string"}, "pasta": {"type": "string"}}}}

    def route(h: Any, method: str, path: str, query: str) -> None:
        q = {k: v[0] for k, v in urllib.parse.parse_qs(query).items()}
        ok = method == "GET" and g.receive(str(q.get("state") or ""), str(q.get("code") or ""), str(q.get("error") or ""))
        page = PAGE.format(title="✅ Pronto" if ok and not q.get("error") else "⚠️ Não deu certo",
                           text=("Pode fechar esta página: o agente avisa no Telegram quando terminar." if ok and not q.get("error") else
                                 "Este link de login venceu ou já foi usado. Peça outro com /google entrar."))
        h._send_html(page)
    service.public_routes = dict(getattr(service, "public_routes", None) or {}, **{"/oauth/google": route})

    def act_command(payload: Dict[str, Any]) -> Dict[str, Any]:
        words = str(payload.get("arg") or "").lower().split()
        if not words:
            return {"text": format_status(g.status())}
        if words[0] == "entrar":
            return g.start_login()
        if words[0] in {"endereco", "endereço", "redirect", "uri"}:
            return {"text": g.redirect_help()}
        if words[0] == "drive" and len(words) > 1 and words[1] in {"on", "off"}:
            service.db.set_setting("google.drive_auto", words[1] == "on")
            return {"text": f"Drive automático {'ligado' if words[1] == 'on' else 'desligado'}."}
        if words[0] == "sair":
            for a in g.accounts():
                vault.drop(f"google:{a['email'].lower()}")
            service.db.set_setting(SETTING, [])
            return {"text": "Contas Google desconectadas. Para revogar de vez: myaccount.google.com → Segurança → Apps de terceiros."}
        if words[0] == "padrao" and len(words) > 1 and words[1] in {"google", "outlook"}:
            service.db.set_setting("agenda_padrao", words[1])
            return {"text": f"Compromissos novos vão para o {calendarios.NAMES[words[1]]}."}
        return {"text": "Use: /google · /google entrar · /google endereco · /google drive on|off · /google padrao google|outlook · "
                        "/google sair"}
    service.extra_actions["google_command"] = act_command
    service.status_providers["google"] = g.status
    return g

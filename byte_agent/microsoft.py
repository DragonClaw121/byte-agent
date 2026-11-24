"""Microsoft 365 / Outlook.com pelo Microsoft Graph — v13.38 (R2.2).

  Agenda do Outlook  entra na agenda unificada (resumo da manhã, "compromisso chegando", "hora de sair", horários
                     livres). Ferramentas: ver compromissos, criar, mover e cancelar — criar/mover/cancelar sempre com
                     aprovação (tipo "calendario" em /permissoes; convites a outras pessoas saem para fora).
  OneDrive           atas, planilhas, documentos e apresentações vão sozinhos para a pasta "Byte" do OneDrive
                     (/microsoft onedrive on|off) e a ferramenta onedrive_salvar manda um arquivo de entregas/.
  Teams              com o login "teams" (/microsoft entrar teams), depois de cada reunião online que você organizou o
                     agente busca a TRANSCRIÇÃO OFICIAL do Teams (com o nome de quem falou) e faz a ata sozinho.
                     Precisa da transcrição ligada na reunião e, em contas de empresa, da aprovação da TI.

Login: o mesmo aplicativo do e-mail (BYTE_MS_CLIENT_ID, criado por scripts/email-conta.sh), com as permissões de agenda e
arquivos: /microsoft entrar. O token de renovação fica criptografado (BYTE_CHAVE_TOKENS), como o do e-mail.
"""
from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

AUTHORITY = "https://login.microsoftonline.com/common/oauth2/v2.0"
GRAPH = "https://graph.microsoft.com/v1.0"
SCOPES = {
    "base": "offline_access https://graph.microsoft.com/User.Read https://graph.microsoft.com/Calendars.ReadWrite "
            "https://graph.microsoft.com/Files.ReadWrite",
    "teams": "offline_access https://graph.microsoft.com/User.Read https://graph.microsoft.com/Calendars.ReadWrite "
             "https://graph.microsoft.com/Files.ReadWrite https://graph.microsoft.com/OnlineMeetings.Read "
             "https://graph.microsoft.com/OnlineMeetingTranscript.Read.All",
}
SETTING = "microsoft.contas"
FOLDER = "Byte"


def _http(method: str, url: str, data: Optional[bytes] = None, headers: Optional[Dict[str, str]] = None,
          timeout: float = 30.0) -> Tuple[int, bytes]:
    req = urllib.request.Request(url, data=data, method=method, headers=dict({"User-Agent": "byte-agent/1.0"}, **(headers or {})))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(20 * 1024 * 1024)
    except urllib.error.HTTPError as err:
        return err.code, err.read(64 * 1024)


def _iso_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _parse_graph_time(value: str) -> datetime:
    return datetime.fromisoformat(str(value)[:19]).replace(tzinfo=timezone.utc)


def parse_vtt(text: str) -> List[Tuple[float, str, str]]:
    """Transcrição do Teams (WebVTT com <v Nome>fala</v>) -> [(segundos, quem, fala)]."""
    out: List[Tuple[float, str, str]] = []
    start = 0.0
    for line in str(text or "").splitlines():
        m = re.match(r"(\d+):(\d\d):(\d\d)[.,](\d+)\s+-->", line.strip())
        if m:
            h, mi, s = int(m.group(1)), int(m.group(2)), int(m.group(3))
            start = h * 3600 + mi * 60 + s
            continue
        v = re.match(r"\s*<v\s+([^>]+)>(.*?)(</v>)?\s*$", line)
        if v:
            out.append((start, v.group(1).strip(), re.sub(r"<[^>]+>", "", v.group(2)).strip()))
    merged: List[Tuple[float, str, str]] = []
    for t, who, said in out:                      # falas seguidas da mesma pessoa viram uma só
        if merged and merged[-1][1] == who and t - merged[-1][0] < 60:
            merged[-1] = (merged[-1][0], who, (merged[-1][2] + " " + said).strip())
        else:
            merged.append((t, who, said))
    return merged


class Microsoft:
    def __init__(self, service: Any, vault: Any, read_secret: Callable[[Tuple[str, ...]], str],
                 http: Callable[..., Tuple[int, bytes]] = _http, clock: Callable[[], float] = time.time):
        self.service, self.db, self.vault, self.read_secret, self.http, self.clock = service, service.db, vault, read_secret, http, clock
        self.pending: Dict[str, Dict[str, Any]] = {}
        self._tokens: Dict[str, Tuple[str, float]] = {}
        self.background_login = True
        self.last_error: Optional[str] = None
        self.notify: Callable[[str], None] = lambda text: self.db.enqueue_proactive_message("microsoft", text, brain_used="MICROSOFT")

    # ---------------- contas
    def accounts(self) -> List[Dict[str, Any]]:
        return [a for a in (self.db.get_setting(SETTING, []) or []) if isinstance(a, dict) and a.get("upn")]

    def account(self, wanted: str = "") -> Dict[str, Any]:
        accs = [a for a in self.accounts() if self.vault.get(f"graph:{a['upn'].lower()}")]
        if not accs:
            raise PermissionError("Nenhuma conta Microsoft conectada para agenda e arquivos: mande /microsoft entrar.")
        if wanted:
            for a in accs:
                if wanted.lower() in {a["upn"].lower(), a.get("id", "")}:
                    return a
        return accs[0]

    def _client(self) -> str:
        cid = self.read_secret(("BYTE_MS_CLIENT_ID",))
        if not cid:
            raise PermissionError("Falta o BYTE_MS_CLIENT_ID (o aplicativo da Microsoft): rode scripts/email-conta.sh com uma "
                                  "conta Outlook, ou veja docs/email.md, parte 'Outlook'.")
        return cid

    def _form(self, url: str, form: Dict[str, str]) -> Tuple[int, Dict[str, Any]]:
        code, body = self.http("POST", url, urllib.parse.urlencode(form).encode(), {"Content-Type": "application/x-www-form-urlencoded"})
        try:
            return code, json.loads(body.decode("utf-8", "replace") or "{}")
        except ValueError:
            return code, {}

    def start_login(self, kind: str = "base") -> Dict[str, Any]:
        kind = "teams" if str(kind).lower().startswith("team") else "base"
        what = "agenda, OneDrive e transcrições do Teams" if kind == "teams" else "agenda e OneDrive"
        browser = getattr(self, "browser_login", None)
        if browser is not None and browser.available():             # v13.54: login pelo navegador
            def failed(msg: str) -> None:
                self.pending.pop(kind, None)
                self.notify("⚠️ Login da Microsoft não concluído: " + msg)
            started = browser.start(SCOPES[kind] + " openid profile", "", lambda data: self._connected(kind, data), failed)
            self.pending[kind] = {"kind": kind, "browser": started["state"]}
            return {"text": f"🔑 Login da Microsoft ({what})\nAbra este link, entre com a sua conta e aceite as permissões:\n"
                            f"{started['url']}\nO link vale 10 minutos. Eu aviso aqui quando terminar.\n"
                            "(Conta de empresa: se pedir \"aprovação do administrador\", é a TI que libera o aplicativo.)",
                    "url": started["url"]}
        code, data = self._form(f"{AUTHORITY}/devicecode", {"client_id": self._client(), "scope": SCOPES[kind]})
        if code != 200 or not data.get("device_code"):
            raise PermissionError(f"A Microsoft recusou o pedido de login: {str(data.get('error_description') or data.get('error') or code)[:240]}")
        state = {"kind": kind, "device_code": data["device_code"], "interval": max(3, int(data.get("interval") or 5)),
                 "expires": self.clock() + int(data.get("expires_in") or 900)}
        self.pending[kind] = state
        if self.background_login:
            threading.Thread(target=self.poll_login, args=(state,), daemon=True, name="ByteLoginGraph").start()
        what = "agenda, OneDrive e transcrições do Teams" if kind == "teams" else "agenda e OneDrive"
        return {"text": f"🔑 Login da Microsoft ({what})\n1. Abra {data.get('verification_uri', 'https://microsoft.com/devicelogin')}\n"
                        f"2. Digite o código: {data.get('user_code')}\n3. Entre com a sua conta e aceite as permissões.\n"
                        "O código vale 15 minutos. Eu aviso aqui quando terminar.\n"
                        "Se a Microsoft recusar (\"método de entrada desatualizado\" ou bloqueado pela empresa), rode "
                        "scripts/microsoft-app.sh no Cloud Shell: ele ativa o login pelo navegador."
                        + ("\n(Em conta de empresa, as transcrições do Teams podem pedir a aprovação da TI.)" if kind == "teams" else ""),
                "user_code": data.get("user_code")}

    def poll_login(self, state: Dict[str, Any], sleep: Callable[[float], None] = time.sleep) -> bool:
        while self.clock() < state["expires"] and self.pending.get(state["kind"]) is state:
            sleep(state["interval"])
            code, data = self._form(f"{AUTHORITY}/token", {"grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                                                           "client_id": self._client(), "device_code": state["device_code"]})
            if code == 200 and data.get("refresh_token"):
                return self._connected(state["kind"], data)
            err = data.get("error")
            if err == "authorization_pending":
                continue
            if err == "slow_down":
                state["interval"] += 5
                continue
            self.pending.pop(state["kind"], None)
            self.notify("⚠️ Login da Microsoft não concluído: " + str(data.get("error_description") or err or code)[:200])
            return False
        self.pending.pop(state["kind"], None)
        return False

    def _connected(self, kind: str, data: Dict[str, Any]) -> bool:
        """Tokens recebidos (navegador ou código): descobre a conta (/me) e guarda o acesso."""
        token = data.get("access_token", "")
        c2, me = self.http("GET", f"{GRAPH}/me?$select=userPrincipalName,mail,displayName", None, {"Authorization": f"Bearer {token}"})
        info = json.loads(me.decode("utf-8", "replace") or "{}") if c2 == 200 else {}
        upn = str(info.get("mail") or info.get("userPrincipalName") or "").strip()
        if not upn:
            self.notify("⚠️ Login da Microsoft feito, mas não consegui ler a conta (/me).")
            return False
        accs = [a for a in self.accounts() if a["upn"].lower() != upn.lower()]
        accs.append({"id": re.sub(r"[^a-z0-9]", "", upn.split("@")[0].lower())[:20] or "ms", "upn": upn,
                     "name": str(info.get("displayName") or ""), "kind": kind})
        self.db.set_setting(SETTING, accs)
        self.vault.put(f"graph:{upn.lower()}", data["refresh_token"])
        self._tokens[upn.lower()] = (token, self.clock() + float(data.get("expires_in") or 3600))
        self.pending.pop(kind, None)
        self.db.record_audit("graph_login", "microsoft", "success", {"kind": kind})
        self.notify(f"✅ Microsoft conectada ({upn}): agenda do Outlook e OneDrive"
                    + (" e transcrições do Teams" if kind == "teams" else "") + ". Veja /compromissos.")
        return True

    def token(self, acc: Dict[str, Any]) -> str:
        key = acc["upn"].lower()
        cached = self._tokens.get(key)
        if cached and cached[1] - 120 > self.clock():
            return cached[0]
        refresh = self.vault.get(f"graph:{key}")
        if not refresh:
            raise PermissionError("A conta Microsoft precisa de login de novo: /microsoft entrar")
        code, data = self._form(f"{AUTHORITY}/token", {"client_id": self._client(), "grant_type": "refresh_token",
                                                       "refresh_token": refresh, "scope": SCOPES.get(acc.get("kind", "base"), SCOPES["base"])})
        if code != 200 or not data.get("access_token"):
            if data.get("error") in {"invalid_grant", "interaction_required"}:
                self.vault.drop(f"graph:{key}")
                raise PermissionError("O login da Microsoft expirou: /microsoft entrar")
            raise PermissionError(f"A Microsoft recusou a renovação: {str(data.get('error_description') or code)[:160]}")
        if data.get("refresh_token"):
            self.vault.put(f"graph:{key}", data["refresh_token"])
        self._tokens[key] = (data["access_token"], self.clock() + float(data.get("expires_in") or 3600))
        return data["access_token"]

    def call(self, acc: Dict[str, Any], method: str, path: str, body: Any = None, raw: Optional[bytes] = None,
             headers: Optional[Dict[str, str]] = None) -> Any:
        h = {"Authorization": f"Bearer {self.token(acc)}"}
        h.update(headers or {})
        data = raw
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            h["Content-Type"] = "application/json"
        code, out = self.http(method, path if path.startswith("https://") else GRAPH + path, data, h)
        if code >= 400:
            try:
                msg = json.loads(out.decode("utf-8", "replace")).get("error", {}).get("message", "")
            except (ValueError, AttributeError):
                msg = out[:160].decode("utf-8", "replace")
            self.last_error = f"HTTP {code}: {msg[:160]}"
            raise PermissionError(f"Microsoft Graph recusou ({self.last_error})")
        self.last_error = None
        if not out:
            return {}
        try:
            return json.loads(out.decode("utf-8", "replace"))
        except ValueError:
            return out

    # ---------------- agenda
    def events(self, start: datetime, end: datetime) -> List[Dict[str, Any]]:
        out = []
        for acc in self.accounts():
            if not self.vault.get(f"graph:{acc['upn'].lower()}"):
                continue
            q = urllib.parse.urlencode({"startDateTime": _iso_utc(start), "endDateTime": _iso_utc(end), "$top": "100",
                                        "$select": "id,subject,start,end,location,isAllDay,isOnlineMeeting,onlineMeeting,isCancelled",
                                        "$orderby": "start/dateTime"})
            data = self.call(acc, "GET", f"/me/calendarView?{q}", headers={"Prefer": 'outlook.timezone="UTC"'})
            for ev in data.get("value") or []:
                if ev.get("isCancelled"):
                    continue
                when = _parse_graph_time(ev["start"]["dateTime"])
                if ev.get("isAllDay"):                                     # dia inteiro: a data, no fuso local
                    from . import gatilhos
                    tz = gatilhos._tz()
                    when = when.replace(tzinfo=tz) if tz else when.replace(tzinfo=None).astimezone()
                out.append({"when": when, "end": _parse_graph_time(ev["end"]["dateTime"]), "all_day": bool(ev.get("isAllDay")),
                            "title": str(ev.get("subject") or "compromisso")[:160],
                            "location": str((ev.get("location") or {}).get("displayName") or "")[:120],
                            "uid": f"ms:{ev['id'][-24:]}", "id": ev["id"], "account": acc["upn"], "source": "outlook",
                            "online_url": str((ev.get("onlineMeeting") or {}).get("joinUrl") or "")})
        return out

    def create_event(self, title: str, start: datetime, end: datetime, location: str = "", attendees: Optional[List[str]] = None,
                     online: bool = False, account: str = "") -> Dict[str, Any]:
        acc = self.account(account)
        body: Dict[str, Any] = {"subject": title[:200], "start": {"dateTime": _iso_utc(start), "timeZone": "UTC"},
                                "end": {"dateTime": _iso_utc(end), "timeZone": "UTC"}}
        if location:
            body["location"] = {"displayName": location[:200]}
        if attendees:
            body["attendees"] = [{"emailAddress": {"address": a}, "type": "required"} for a in attendees[:20]]
        if online:
            body["isOnlineMeeting"] = True
            body["onlineMeetingProvider"] = "teamsForBusiness"
        res = self.call(acc, "POST", "/me/events", body)
        self.db.record_audit("graph_event_created", "microsoft", "success", {"attendees": len(attendees or [])})
        return {"id": res.get("id"), "link": res.get("webLink"), "online_url": (res.get("onlineMeeting") or {}).get("joinUrl")}

    def move_event_id(self, account: str, eid: str, start: datetime, end: datetime) -> Dict[str, Any]:
        self.call(self.account(account), "PATCH", f"/me/events/{urllib.parse.quote(eid)}",
                  {"start": {"dateTime": _iso_utc(start), "timeZone": "UTC"}, "end": {"dateTime": _iso_utc(end), "timeZone": "UTC"}})
        return {"moved": True}

    def cancel_event_id(self, account: str, eid: str) -> Dict[str, Any]:
        self.call(self.account(account), "DELETE", f"/me/events/{urllib.parse.quote(eid)}")
        return {"cancelled": True}

    def move_event(self, ref: str, start: datetime, end: datetime) -> Dict[str, Any]:
        from . import calendarios
        _, account, eid = calendarios.resolve(self.service, ref)
        return self.move_event_id(account, eid, start, end)

    def cancel_event(self, ref: str) -> Dict[str, Any]:
        from . import calendarios
        _, account, eid = calendarios.resolve(self.service, ref)
        return self.cancel_event_id(account, eid)

    # ---------------- OneDrive
    def upload(self, path: Path, folder: str = FOLDER, account: str = "") -> Dict[str, Any]:
        acc = self.account(account)
        data = Path(path).read_bytes()
        if len(data) > 250 * 1024 * 1024:
            raise ValueError("Arquivo acima de 250 MB.")
        name = urllib.parse.quote(Path(path).name)
        res = self.call(acc, "PUT", f"/me/drive/root:/{urllib.parse.quote(folder)}/{name}:/content", raw=data,
                        headers={"Content-Type": "application/octet-stream"})
        self.db.record_audit("graph_upload", "microsoft", "success", {"file": Path(path).name})
        return {"name": res.get("name"), "link": res.get("webUrl")}

    def auto_upload_on(self) -> bool:
        return bool(self.db.get_setting("microsoft.onedrive_auto", True)) and bool(self.accounts())

    # ---------------- Teams: transcrições oficiais
    def fetch_teams_transcripts(self, since_hours: float = 6.0) -> List[Dict[str, Any]]:
        """Transcrições de reuniões que você ORGANIZOU, terminadas nas últimas horas, que ainda não viraram ata."""
        done = set(self.db.get_setting("microsoft.transcricoes_feitas", []) or [])
        made = []
        for acc in [a for a in self.accounts() if a.get("kind") == "teams" and self.vault.get(f"graph:{a['upn'].lower()}")]:
            me = self.call(acc, "GET", "/me?$select=id")
            uid = me.get("id")
            start = datetime.fromtimestamp(self.clock() - since_hours * 3600, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            url = (f"/users/{uid}/onlineMeetings/getAllTranscripts(meetingOrganizerUserId='{uid}',"
                   f"startDateTime={start})")
            for tr in (self.call(acc, "GET", url).get("value") or [])[:10]:
                tid = str(tr.get("id") or "")
                if not tid or tid in done:
                    continue
                content = self.call(acc, "GET", f"/users/{uid}/onlineMeetings/{tr.get('meetingId')}/transcripts/{tid}/content?$format=text/vtt")
                vtt = content.decode("utf-8", "replace") if isinstance(content, bytes) else json.dumps(content)
                lines = parse_vtt(vtt)
                meetings = getattr(self.service, "meetings", None)
                if meetings is None or not lines:
                    done.add(tid)
                    continue
                title = "Reunião do Teams"
                try:
                    info = self.call(acc, "GET", f"/users/{uid}/onlineMeetings/{tr.get('meetingId')}?$select=subject")
                    title = str(info.get("subject") or title)[:120]
                except PermissionError:
                    pass
                m = meetings.start(title, source="teams")
                text = "".join(f"[{int(t) // 60:02d}:{int(t) % 60:02d}] {who}: {said}\n" for t, who, said in lines)
                with self.db.connection() as conn:
                    conn.execute("UPDATE meetings SET transcript = ?, seconds = ? WHERE id = ?", (text, lines[-1][0], m["id"]))
                meetings.finish(m["id"], background=False)
                done.add(tid)
                made.append(m)
        self.db.set_setting("microsoft.transcricoes_feitas", sorted(done)[-200:])
        return made

    def status(self) -> Dict[str, Any]:
        return {"accounts": [{"upn": a["upn"], "kind": a.get("kind", "base"), "ready": bool(self.vault.get(f"graph:{a['upn'].lower()}"))}
                             for a in self.accounts()],
                "onedrive_auto": bool(self.db.get_setting("microsoft.onedrive_auto", True)), "last_error": self.last_error,
                "login_pending": list(self.pending)}


def format_status(st: Dict[str, Any]) -> str:
    if not st["accounts"]:
        return ("Nenhuma conta Microsoft conectada para agenda e arquivos.\n/microsoft entrar — agenda do Outlook e OneDrive\n"
                "/microsoft entrar teams — também as transcrições do Teams (conta de empresa)")
    lines = ["🟦 Microsoft:"]
    for a in st["accounts"]:
        lines.append(f"• {a['upn']} — {'✅' if a['ready'] else '🔑 login expirado'} agenda e OneDrive"
                     + (" + transcrições do Teams" if a["kind"] == "teams" else ""))
    lines.append(f"OneDrive automático (atas, planilhas, documentos na pasta \"{FOLDER}\"): {'ligado' if st['onedrive_auto'] else 'desligado'}")
    if st.get("last_error"):
        lines.append(f"⚠️ {st['last_error']}")
    lines.append("Comandos: /compromissos [hoje|amanha|semana] · /microsoft onedrive on|off · /microsoft entrar [teams]")
    return "\n".join(lines)


def install(service: Any, data_dir: Path) -> Microsoft:
    from . import calendarios, engine as _engine
    from .caixas import TokenVault
    from .relogio import cached
    hub = getattr(service, "mail_hub", None)
    vault = hub.vault if hub is not None else TokenVault(Path(data_dir).expanduser() / "segredos" / "tokens-email.bin",
                                                         _engine._read_secret(("BYTE_CHAVE_TOKENS",)))
    ms = Microsoft(service, vault, _engine._read_secret)
    service.microsoft = ms
    from . import ms_login                                    # v13.54: login pelo navegador (rota em caixas.install)
    folder = ms_login.handoff_dir(data_dir)
    ms.browser_login = ms_login.BrowserLogin(folder, ms._client, _engine._read_secret, ms._form)
    if "/oauth/microsoft" not in (getattr(service, "public_routes", None) or {}):
        ms_login.register_route(service, folder)

    def provider(start: datetime, end: datetime) -> List[Dict[str, Any]]:
        if not ms.accounts():
            return []
        key = f"graph-cal:{int(start.timestamp()) // 60}:{int(end.timestamp()) // 60}"
        return cached(key, 180.0, lambda: ms.events(start, end))
    provider.active = lambda: bool(ms.accounts())          # type: ignore[attr-defined]
    service.calendar_providers = list(getattr(service, "calendar_providers", []) or []) + [provider]

    def on_output(path: Path) -> None:                     # entregas novas vão para o OneDrive (se ligado)
        if ms.auto_upload_on():
            try:
                ms.upload(path)
            except Exception as err:
                ms.last_error = f"OneDrive: {type(err).__name__}: {str(err)[:120]}"
    service.output_hooks = list(getattr(service, "output_hooks", []) or []) + [on_output]

    # ---------------- escrita na agenda (as ferramentas são comuns: calendarios.install_tools)
    class Writer:
        ready = staticmethod(lambda: any(ms.vault.get(f"graph:{a['upn'].lower()}") for a in ms.accounts()))

        @staticmethod
        def create(title: str, start: datetime, end: datetime, location: str, attendees: List[str], online: bool) -> Dict[str, Any]:
            return ms.create_event(title, start, end, location, attendees, online)

        @staticmethod
        def move(account: str, eid: str, start: datetime, end: datetime) -> Any:
            return ms.move_event_id(account, eid, start, end)

        @staticmethod
        def cancel(account: str, eid: str) -> Any:
            return ms.cancel_event_id(account, eid)
    service.calendar_writers = dict(getattr(service, "calendar_writers", {}) or {}, outlook=Writer())
    calendarios.install_tools(service)

    def t_upload(args: Dict[str, Any], turn: Dict[str, Any]) -> str:
        provider_file = getattr(service, "file_provider", None)
        path = provider_file(str(args.get("arquivo") or "")) if callable(provider_file) else None
        if path is None:
            return "ERRO: arquivo não encontrado em entregas/ (use o nome exato de um arquivo gerado)."
        try:
            res = ms.upload(path, str(args.get("pasta") or FOLDER))
        except (ValueError, PermissionError) as err:
            return f"ERRO: {err}"
        return json.dumps({"salvo": True, "link": res.get("link")}, ensure_ascii=False)

    service.extra_tools.update({
        "onedrive_salvar": {"auto": True, "mission": True, "handler": t_upload,
                            "intent": re.compile(r"\b(onedrive|one drive|nuvem|salv\w+ (?:no|na)|sharepoint)\b"),
                            "description": "Salva no OneDrive (pasta Byte) um arquivo gerado em entregas/.",
                            "parameters": {"type": "object", "required": ["arquivo"], "properties": {
                                "arquivo": {"type": "string"}, "pasta": {"type": "string"}}}},
    })

    def act_command(payload: Dict[str, Any]) -> Dict[str, Any]:
        words = str(payload.get("arg") or "").lower().split()
        if not words:
            return {"text": format_status(ms.status())}
        if words[0] == "entrar":
            return ms.start_login(words[1] if len(words) > 1 else "base")
        if words[0] == "onedrive" and len(words) > 1 and words[1] in {"on", "off"}:
            service.db.set_setting("microsoft.onedrive_auto", words[1] == "on")
            return {"text": f"OneDrive automático {'ligado' if words[1] == 'on' else 'desligado'}."}
        if words[0] == "sair":
            for a in ms.accounts():
                vault.drop(f"graph:{a['upn'].lower()}")
            service.db.set_setting(SETTING, [])
            return {"text": "Contas Microsoft (agenda e OneDrive) desconectadas."}
        return {"text": "Use: /microsoft · /microsoft entrar · /microsoft entrar teams · /microsoft onedrive on|off · /microsoft sair"}

    service.extra_actions["microsoft_command"] = act_command
    service.status_providers["microsoft"] = ms.status
    sched = getattr(service, "scheduler", None)
    if sched is not None:
        sched.add("teams", 900, lambda: ms.fetch_teams_transcripts(),
                  when=lambda: any(a.get("kind") == "teams" for a in ms.accounts()), label="Transcrições do Teams")
    return ms

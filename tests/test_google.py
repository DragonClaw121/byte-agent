"""v13.39 (R2.3): Google Agenda e Drive — login com volta para /oauth/google (entre processos), agenda unificada com o
Outlook, criar com Meet (com aprovação) e Drive automático."""
import base64
import json
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import pytest

from byte_agent import engine, gatilhos, google_agenda, headless

TZ = gatilhos._tz()


def id_token(email):
    body = base64.urlsafe_b64encode(json.dumps({"email": email}).encode()).decode().rstrip("=")
    return f"x.{body}.y"


class FakeGoogle:
    def __init__(self):
        self.calls, self.created, self.folders, self.uploads = [], [], [], []
        self.soon = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(minutes=25)

    def __call__(self, method, url, data=None, headers=None, timeout=30.0):
        self.calls.append((method, url))
        u = urlparse(url)
        if url == google_agenda.TOKEN:
            form = parse_qs(data.decode())
            if form["grant_type"][0] == "authorization_code":
                assert form["code"] == ["CODIGO"] and form["code_verifier"][0] and form["client_secret"] == ["segredo"]
                return 200, json.dumps({"access_token": "GAT", "refresh_token": "GRT", "expires_in": 3600,
                                        "id_token": id_token("gabriel@gmail.com")}).encode()
            return 200, json.dumps({"access_token": "GAT", "expires_in": 3600}).encode()
        assert headers["Authorization"] == "Bearer GAT"
        if u.path == "/calendar/v3/calendars/primary/events" and method == "GET":
            return 200, json.dumps({"items": [
                {"id": "gev1", "summary": "Dentista", "location": "Rua X, 10", "start": {"dateTime": self.soon.isoformat().replace("+00:00", "Z")},
                 "end": {"dateTime": (self.soon + timedelta(hours=1)).isoformat().replace("+00:00", "Z")}},
                {"id": "gev2", "summary": "Aniversário", "start": {"date": self.soon.strftime("%Y-%m-%d")}, "end": {"date": "x"}},
                {"id": "gev3", "summary": "Cancelado", "status": "cancelled", "start": {"date": "2026-01-01"}}]}).encode()
        if u.path == "/calendar/v3/calendars/primary/events" and method == "POST":
            self.created.append((parse_qs(u.query), json.loads(data)))
            return 200, json.dumps({"id": "novo", "hangoutLink": "https://meet.google.com/abc-defg-hij"}).encode()
        if u.path == "/drive/v3/files" and method == "GET":
            return 200, json.dumps({"files": []}).encode()
        if u.path == "/drive/v3/files" and method == "POST":
            self.folders.append(json.loads(data))
            return 200, json.dumps({"id": "PASTA"}).encode()
        if u.path == "/upload/drive/v3/files":
            self.uploads.append(data)
            return 200, json.dumps({"name": "x", "webViewLink": "https://drive/x"}).encode()
        raise AssertionError((method, url))


@pytest.fixture()
def svc(tmp_path, monkeypatch):
    from test_native_tools import TOOL_MODEL, FakeOllama
    for k, v in {"BYTE_GOOGLE_CLIENT_ID": "id.apps.googleusercontent.com", "BYTE_GOOGLE_CLIENT_SECRET": "segredo",
                 "BYTE_ENDERECO_PUBLICO": "https://kronus.exemplo.com", "BYTE_CHAVE_TOKENS": "chave",
                 "BYTE_DADOS_PERSISTENTES": str(tmp_path / "store")}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("BYTE_AGENDA_ICS", raising=False)
    fake = FakeOllama()
    service = headless.build_service(tmp_path / "bit", ollama_url=fake.url, model=TOOL_MODEL, profile="trabalho")
    g = FakeGoogle()
    service.google.http, service.google.background_login = g, False
    service.fakeg = g
    yield service
    service.shutdown()
    fake.httpd.shutdown()
    engine.apply_profile("pessoal")


class Page:
    def __init__(self):
        self.html = ""

    def _send_html(self, html):
        self.html = html


def login(svc, other=None):
    out = svc.google.start_login()
    q = parse_qs(urlparse(out["url"]).query)
    assert q["redirect_uri"] == ["https://kronus.exemplo.com/oauth/google"] and q["code_challenge_method"] == ["S256"]
    assert "calendar.events" in q["scope"][0] and "drive.file" in q["scope"][0] and q["access_type"] == ["offline"]
    receiver = other or svc                                     # a volta do Google pode cair em OUTRO processo (o Kronus)
    page = Page()
    receiver.public_routes["/oauth/google"](page, "GET", "/oauth/google", f"state={out['state']}&code=CODIGO")
    assert "✅ Pronto" in page.html
    page2 = Page()
    receiver.public_routes["/oauth/google"](page2, "GET", "/oauth/google", f"state={out['state']}&code=OUTRO")
    assert "venceu ou já foi usado" in page2.html                 # uso único
    assert svc.google.wait_login(out["state"], sleep=lambda s: None)


def test_login_through_another_process_and_forged_state(svc, tmp_path):
    from test_native_tools import TOOL_MODEL, FakeOllama
    fake = FakeOllama()
    kronus = headless.build_service(tmp_path / "kronus", ollama_url=fake.url, model=TOOL_MODEL, profile="pessoal", web=False)
    try:
        page = Page()
        kronus.public_routes["/oauth/google"](page, "GET", "/oauth/google", "state=" + "a" * 30 + "&code=X")
        assert "venceu" in page.html                               # state inventado não entra
        login(svc, other=kronus)
        assert [a["email"] for a in svc.google.accounts()] == ["gabriel@gmail.com"] and b"GRT" not in svc.google.vault.path.read_bytes()
        assert "gabriel@gmail.com — ✅ agenda e Drive" in svc.extra_actions["google_command"]({"arg": ""})["text"]
    finally:
        kronus.proactive_engine.stop()
        fake.httpd.shutdown()


def test_google_calendar_joins_the_unified_agenda(svc):
    login(svc)
    sent = svc.trigger_engine.tick(force=True)
    assert any(t.startswith("📅 Em 25 min") and "Dentista — Rua X, 10" in t for t in sent)
    text = svc.extra_actions["calendar_list"]({"periodo": "hoje"})["text"]
    assert "Dentista" in text and "dia todo — Aniversário" in text and "Cancelado" not in text


def test_create_with_meet_needs_approval(svc):
    login(svc)
    turn = {"blocks": [], "executions": []}
    out = svc.run_agent_tool("agenda_criar_evento", {"titulo": "Call", "inicio": "2026-11-26 09:00", "convidados": "a@b.com",
                                                     "online": True, "agenda": "google"}, turn)
    assert out.startswith("AGUARDANDO APROVAÇÃO") and not svc.fakeg.created
    card = json.loads(turn["blocks"][0].split("\n", 1)[1].rsplit("```", 1)[0])
    svc.approve_pending_action(card["id"], {"title": card["title"], "details": card["details"]})
    q, body = svc.fakeg.created[0]
    assert q["sendUpdates"] == ["all"] and q["conferenceDataVersion"] == ["1"]
    assert body["start"]["dateTime"].startswith("2026-11-26T09:00:00-03:00") and body["attendees"] == [{"email": "a@b.com"}]
    assert body["conferenceData"]["createRequest"]["conferenceSolutionKey"]["type"] == "hangoutsMeet"


def test_drive_auto_upload_creates_the_folder(svc):
    login(svc)
    svc.run_agent_tool("create_document", {"titulo": "Proposta", "secoes": [{"titulo": "Escopo", "paragrafos": ["x"]}]},
                       {"blocks": [], "executions": []})
    for _ in range(60):
        if svc.fakeg.uploads:
            break
        time.sleep(0.05)
    assert svc.fakeg.folders == [{"name": "Byte", "mimeType": "application/vnd.google-apps.folder"}]
    up = svc.fakeg.uploads[0]
    assert b'"parents": ["PASTA"]' in up and b'"name": "documento_proposta_' in up and b"PK" in up


def test_redirect_help_shows_the_exact_uri(tmp_path, monkeypatch):
    """v13.55: redirect_uri_mismatch — /google endereco mostra exatamente o que o agente manda ao Google."""
    monkeypatch.setenv("BYTE_GOOGLE_REDIRECT", "  https://byte.exemplo.io/oauth/google \n")
    monkeypatch.setenv("BYTE_GOOGLE_CLIENT_ID", "123-abc.apps.googleusercontent.com")
    from types import SimpleNamespace
    from byte_agent import google_agenda
    import os
    read = lambda names: next((os.environ.get(n, "") for n in names if os.environ.get(n)), "")
    g = google_agenda.Google(SimpleNamespace(db=SimpleNamespace(enqueue_proactive_message=lambda *a, **k: None)), None, read, tmp_path)
    assert g.redirect_uri() == "https://byte.exemplo.io/oauth/google"
    text = g.redirect_help()
    assert "https://byte.exemplo.io/oauth/google\n" in text and "123-abc.apps.googleusercontent.com" in text
    assert "URIs de redirecionamento autorizados" in text


def test_test_mode_login_expiry_is_announced_a_day_before(svc):
    """v13.56: app do Google em "Teste" (não publicado): o login cai a cada 7 dias; o agente avisa antes, uma vez."""
    login(svc)
    g, notes = svc.google, []
    g.notify = notes.append
    assert g.remind_expiry() == []                                    # recém-conectado
    since = g.accounts()[0]["desde"]
    g.clock = lambda: since + 6.2 * 86400
    assert g.remind_expiry() == ["gabriel@gmail.com"] and "/google entrar" in notes[-1]
    assert g.remind_expiry() == []                                    # uma vez por login
    assert "app publicado" in svc.extra_actions["google_command"]({"arg": "teste off"})["text"]
    accs = g.accounts()
    accs[0].pop("avisado")
    svc.db.set_setting("google.contas", accs)
    assert not g.test_mode() and g.remind_expiry() == []              # publicado: sem aviso

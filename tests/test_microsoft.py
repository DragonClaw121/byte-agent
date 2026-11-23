"""v13.38 (R2.2): Microsoft Graph — agenda do Outlook na agenda unificada, criar/mover/cancelar com aprovação,
OneDrive automático e transcrições oficiais do Teams virando ata."""
import json
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, unquote, urlparse

import pytest

from byte_agent import engine, gatilhos, headless, microsoft, permissoes

TZ = gatilhos._tz()
VTT = """WEBVTT

00:00:01.000 --> 00:00:05.000
<v Ana Souza>Bom dia, vamos revisar o orçamento.</v>

00:00:05.500 --> 00:00:09.000
<v Ana Souza>Proponho aprovar 50 mil.</v>

00:00:10.000 --> 00:00:14.000
<v João Lima>Concordo. Eu envio o contrato até sexta.</v>
"""


class FakeGraph:
    def __init__(self):
        self.calls, self.polls, self.events = [], 0, []
        now = datetime.now(timezone.utc).replace(microsecond=0)
        self.soon = now + timedelta(minutes=20)

    def __call__(self, method, url, data=None, headers=None, timeout=30.0):
        self.calls.append((method, url, data, dict(headers or {})))
        u = urlparse(url)
        form = parse_qs(data.decode()) if data and (headers or {}).get("Content-Type", "").startswith("application/x-www") else {}
        if u.path.endswith("/devicecode"):
            return 200, json.dumps({"device_code": "D", "user_code": "XYZ-789", "verification_uri": "https://microsoft.com/devicelogin",
                                    "interval": 1, "expires_in": 900}).encode()
        if u.path.endswith("/token"):
            if form.get("grant_type", [""])[0].endswith("device_code"):
                self.polls += 1
                if self.polls == 1:
                    return 400, b'{"error": "authorization_pending"}'
            return 200, json.dumps({"access_token": "AT", "refresh_token": "RT-" + str(self.polls), "expires_in": 3600}).encode()
        assert (headers or {}).get("Authorization") == "Bearer AT"
        if u.path == "/v1.0/me":
            return 200, json.dumps({"userPrincipalName": "ana@empresa.com", "mail": "ana@empresa.com", "displayName": "Ana", "id": "U1"}).encode()
        if u.path == "/v1.0/me/calendarView":
            fmt = lambda d: d.strftime("%Y-%m-%dT%H:%M:%S.0000000")
            return 200, json.dumps({"value": [
                {"id": "AAMkEVENTO1" + "x" * 40, "subject": "Reunião de orçamento", "start": {"dateTime": fmt(self.soon)},
                 "end": {"dateTime": fmt(self.soon + timedelta(hours=1))}, "location": {"displayName": ""}, "isAllDay": False,
                 "isOnlineMeeting": True, "onlineMeeting": {"joinUrl": "https://teams.microsoft.com/l/x"}},
                {"id": "AAMkEVENTO2" + "y" * 40, "subject": "Cancelado", "start": {"dateTime": fmt(self.soon)},
                 "end": {"dateTime": fmt(self.soon)}, "isCancelled": True}]}).encode()
        if u.path == "/v1.0/me/events" and method == "POST":
            self.events.append(json.loads(data))
            return 201, json.dumps({"id": "NOVO", "webLink": "https://outlook/x"}).encode()
        if u.path.startswith("/v1.0/me/events/") and method in {"PATCH", "DELETE"}:
            return (200 if method == "PATCH" else 204), b"{}" if method == "PATCH" else b""
        if u.path.startswith("/v1.0/me/drive/root:"):
            return 201, json.dumps({"name": unquote(u.path).rsplit("/", 1)[-1].replace(":", ""), "webUrl": "https://onedrive/x"}).encode()
        if "getAllTranscripts" in u.path:
            return 200, json.dumps({"value": [{"id": "T1", "meetingId": "M1"}]}).encode()
        if u.path.endswith("/transcripts/T1/content"):
            return 200, VTT.encode()
        if u.path.endswith("/onlineMeetings/M1"):
            return 200, json.dumps({"subject": "Orçamento 2027 (Teams)"}).encode()
        raise AssertionError((method, url))


@pytest.fixture()
def svc(tmp_path, monkeypatch):
    from test_native_tools import TOOL_MODEL, FakeOllama
    monkeypatch.setenv("BYTE_MS_CLIENT_ID", "00000000-1111-2222-3333-444444444444")
    monkeypatch.setenv("BYTE_CHAVE_TOKENS", "chave")
    monkeypatch.delenv("BYTE_AGENDA_ICS", raising=False)
    fake = FakeOllama()
    service = headless.build_service(tmp_path, ollama_url=fake.url, model=TOOL_MODEL, profile="trabalho")
    graph = FakeGraph()
    service.microsoft.http, service.microsoft.background_login = graph, False
    service.graph = graph
    yield service
    service.shutdown()
    fake.httpd.shutdown()
    engine.apply_profile("pessoal")


def login(svc, kind="base"):
    out = svc.microsoft.start_login(kind)
    assert "XYZ-789" in out["text"]
    state = svc.microsoft.pending["teams" if kind == "teams" else "base"]
    assert svc.microsoft.poll_login(state, sleep=lambda s: None)


def test_login_and_unified_calendar(svc):
    from byte_agent import calendarios
    assert not calendarios.has_providers(svc)                             # sem conta: agenda "não configurada"
    login(svc)
    assert svc.microsoft.accounts()[0]["upn"] == "ana@empresa.com" and calendarios.has_providers(svc)
    assert b"RT-" not in svc.mail_hub.vault.path.read_bytes()                # token criptografado
    now = datetime.now(TZ)
    evs = calendarios.provider_events(svc, now - timedelta(hours=1), now + timedelta(hours=3))
    assert [e["title"] for e in evs] == ["Reunião de orçamento"] and evs[0]["online_url"]
    sent = svc.trigger_engine.tick(force=True)
    assert any(t.startswith("📅 Em 20 min") and "Reunião de orçamento (online)" in t for t in sent)
    text = svc.extra_actions["calendar_list"]({"periodo": "hoje"})["text"]
    assert "[e1]" in text and "Reunião de orçamento" in text and "[online]" in text


def test_create_move_cancel_need_approval(svc):
    login(svc)
    assert permissoes.levels(svc.db)["calendario"] == "aprovar"
    with pytest.raises(PermissionError):
        permissoes.set_level(svc.db, "calendario", "automatico")
    turn = {"blocks": [], "executions": []}
    out = svc.run_agent_tool("agenda_criar_evento", {"titulo": "Call com cliente", "inicio": "2026-11-25 14:30",
                                                     "duracao_min": 30, "convidados": "cliente@x.com", "online": True}, turn)
    assert out.startswith("AGUARDANDO APROVAÇÃO") and not svc.graph.events
    card = json.loads(turn["blocks"][0].split("\n", 1)[1].rsplit("```", 1)[0])
    assert "Criar o compromisso \"Call com cliente\" em 2026-11-25 14:30, com cliente@x.com (Teams)" in card["details"] + card["title"]
    svc.approve_pending_action(card["id"], {"title": card["title"], "details": card["details"]})
    ev = svc.graph.events[0]
    assert ev["start"] == {"dateTime": "2026-11-25T17:30:00", "timeZone": "UTC"} and ev["end"]["dateTime"] == "2026-11-25T18:00:00"
    assert ev["attendees"][0]["emailAddress"]["address"] == "cliente@x.com" and ev["isOnlineMeeting"]
    svc.extra_actions["calendar_list"]({"periodo": "hoje"})
    moved = svc.microsoft.move_event("e1", datetime(2026, 11, 26, 10, tzinfo=TZ), datetime(2026, 11, 26, 11, tzinfo=TZ))
    assert moved["moved"] and svc.graph.calls[-1][0] == "PATCH"
    assert svc.microsoft.cancel_event("e1")["cancelled"] and svc.graph.calls[-1][0] == "DELETE"
    with pytest.raises(ValueError):
        svc.microsoft.cancel_event("e99")


def test_onedrive_auto_upload_of_deliveries(svc):
    login(svc)
    svc.run_agent_tool("create_spreadsheet", {"titulo": "Gastos", "colunas": ["Item", {"nome": "Valor", "tipo": "moeda"}],
                                              "linhas": [["Luz", "100"]]}, {"blocks": [], "executions": []})
    for _ in range(50):
        if any(c[0] == "PUT" for c in svc.graph.calls):
            break
        time.sleep(0.05)
    put = next(c for c in svc.graph.calls if c[0] == "PUT")
    assert "/me/drive/root:/Byte/planilha_gastos_" in unquote(put[1]) and put[2][:2] == b"PK"   # o .xlsx
    svc.extra_actions["microsoft_command"]({"arg": "onedrive off"})
    assert not svc.microsoft.auto_upload_on()


def test_teams_transcript_becomes_minutes_with_speakers(svc):
    assert microsoft.parse_vtt(VTT) == [(1.0, "Ana Souza", "Bom dia, vamos revisar o orçamento. Proponho aprovar 50 mil."),
                                        (10.0, "João Lima", "Concordo. Eu envio o contrato até sexta.")]
    login(svc, "teams")
    svc.meetings._llm = lambda messages, n: json.dumps({"resumo": "Orçamento aprovado.", "participantes": ["Ana Souza", "João Lima"],
                                                        "decisoes": ["Aprovar 50 mil"], "tarefas": [{"o_que": "Enviar o contrato",
                                                        "quem": "João Lima", "prazo": "sexta"}], "pontos_em_aberto": [], "proximos_passos": []})
    made = svc.microsoft.fetch_teams_transcripts()
    assert len(made) == 1 and svc.microsoft.fetch_teams_transcripts() == []              # não refaz a mesma
    m = svc.meetings.get(made[0]["id"])
    assert m["title"] == "Orçamento 2027 (Teams)" and m["source"] == "teams" and "[00:10] João Lima: Concordo." in m["transcript"]
    assert m["status"] == "pronta" and "Enviar o contrato — João Lima (até sexta)" in m["ata_text"]
    assert "teams" in svc.scheduler.jobs


def test_telegram_commands(svc):
    from byte_agent.telegram_bridge import TelegramBridge
    from test_telegram_bridge import OWNER, TOKEN, FakeTelegram
    tg = FakeTelegram()
    bridge = TelegramBridge(svc, token=TOKEN, allowed_chat_ids=[OWNER], api_base=tg.url)
    try:
        def cmd(text, n=[0]):
            n[0] += 1
            bridge.handle_update({"update_id": n[0], "message": {"chat": {"id": OWNER}, "text": text}})
            return tg.sent[-1]["text"]
        assert "/microsoft entrar" in cmd("/microsoft")
        assert "XYZ-789" in cmd("/microsoft entrar")
        assert svc.microsoft.poll_login(svc.microsoft.pending["base"], sleep=lambda s: None)
        assert "ana@empresa.com" in cmd("/microsoft")
        assert cmd("/compromissos hoje").startswith("📅 [e1]")
    finally:
        bridge.stop()
        tg.httpd.shutdown()

"""v13.14: gatilhos por evento — agenda iCal, e-mail novo, tarefa parada, objetivo travado."""
from datetime import datetime, timedelta, timezone

import pytest

from byte_agent import engine, gatilhos, headless
from test_native_tools import TOOL_MODEL, FakeOllama

SP = gatilhos.ZoneInfo("America/Sao_Paulo")


def ts(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=SP).timestamp()


ICS = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:unico-1
DTSTART:20261120T170000Z
SUMMARY:Reunião com cliente\\, ACME
LOCATION:Sala 2
END:VEVENT
BEGIN:VEVENT
UID:semanal-1
DTSTART;TZID=America/Sao_Paulo:20261102T090000
RRULE:FREQ=WEEKLY;BYDAY=MO,WE;UNTIL=20261231T235959Z
EXDATE;TZID=America/Sao_Paulo:20261118T090000
SUMMARY:Daily do time
END:VEVENT
BEGIN:VEVENT
UID:dia-todo
DTSTART;VALUE=DATE:20261120
SUMMARY:Feriado
END:VEVENT
BEGIN:VEVENT
UID:cancelado
DTSTART:20261120T171000Z
SUMMARY:Cancelada
STATUS:CANCELLED
END:VEVENT
END:VCALENDAR
"""


def test_ics_parsing_with_timezones_recurrence_and_exceptions():
    events = {e["uid"]: e for e in gatilhos.parse_ics(ICS)}
    assert set(events) == {"unico-1", "semanal-1", "dia-todo"}               # cancelado fica de fora
    assert events["unico-1"]["title"] == "Reunião com cliente, ACME"
    assert events["unico-1"]["start"].timestamp() == ts(2026, 11, 20, 14)    # 17h UTC = 14h em Brasília
    weekly = events["semanal-1"]
    week = gatilhos.occurrences(weekly, datetime(2026, 11, 16, tzinfo=SP), datetime(2026, 11, 23, tzinfo=SP))
    assert [o.strftime("%a %d %H:%M") for o in week] == ["Mon 16 09:00"]       # quarta 18 foi exceção (EXDATE)
    after = gatilhos.occurrences(weekly, datetime(2027, 1, 1, tzinfo=SP), datetime(2027, 2, 1, tzinfo=SP))
    assert after == []                                                        # UNTIL respeitado
    assert events["dia-todo"]["all_day"] is True


class FakeService:
    def __init__(self, db):
        self.db = db
        self.email_client = None
        self.goal_runner = None


@pytest.fixture()
def db(tmp_path):
    return engine.UnifiedDatabase(tmp_path / "byte.db", engine.VectorEngine())


def make(db, now, ics=ICS, mails=None, env=None):
    sent = []
    clock = {"t": now}
    svc = FakeService(db)
    secrets = dict({"BYTE_AGENDA_ICS": "https://calendar.example/secreto.ics"}, **(env or {}))
    trig = gatilhos.TriggerEngine(svc, sent.append, read_secret=lambda names: next((secrets.get(n, "") for n in names), ""),
                                  clock=lambda: clock["t"], ics_fetcher=lambda url: ics,
                                  mail_fetcher=lambda: list(mails if mails is not None else []))
    return trig, sent, clock


def test_agenda_warns_once_before_each_meeting(db, monkeypatch):
    monkeypatch.setenv("BYTE_GATILHO_AGENDA_MIN", "30")
    trig, sent, clock = make(db, ts(2026, 11, 20, 13, 40))                    # 20 min antes da reunião das 14h
    assert trig.tick() == sent and len(sent) == 1
    assert sent[0] == "📅 Em 20 min (14:00): Reunião com cliente, ACME — Sala 2"
    clock["t"] += 700                                                         # passou o intervalo do vigia
    assert trig.tick() == [] and len(sent) == 1                               # não repete o mesmo aviso
    clock["t"] = ts(2026, 11, 23, 8, 45)                                      # segunda: daily das 9h
    assert trig.tick() == ["📅 Em 15 min (09:00): Daily do time"]
    clock["t"] = ts(2026, 11, 18, 8, 45)
    trig.last_run.clear()
    assert trig.tick() == []                                                  # quarta 18 era exceção


def test_email_first_run_is_a_baseline_then_new_messages_warn(db, monkeypatch):
    mails = [{"uid": "10", "from": "Loja <promo@loja.com>", "subject": "Ofertas"}]
    trig, sent, clock = make(db, 1000.0, mails=mails)
    trig.service.email_client = type("M", (), {"status": lambda self: {"credentials_ready": True}})()
    trig.tick()
    assert sent == []                                                         # o que já estava não lido não vira aviso
    mails += [{"uid": "11", "from": "Chefe <chefe@empresa.com>", "subject": "Relatório urgente"},
              {"uid": "12", "from": "Ana", "subject": "Almoço?"}]
    clock["t"] += 1000
    trig.tick()
    assert sent[-1].startswith("📬 2 e-mails novos:") and "Chefe <chefe@empresa.com>: Relatório urgente" in sent[-1]
    monkeypatch.setenv("BYTE_GATILHO_EMAIL_FILTRO", "chefe@empresa.com, urgente")
    mails += [{"uid": "13", "from": "Newsletter", "subject": "Novidades"},
              {"uid": "14", "from": "chefe@empresa.com", "subject": "Ligação"}]
    clock["t"] += 1000
    trig.tick()
    assert sent[-1] == "📬 E-mail novo de chefe@empresa.com: Ligação"         # newsletter filtrada
    clock["t"] += 1000
    assert trig.tick() == []


def test_stale_tasks_and_blocked_goals(db, tmp_path):
    from byte_agent import objetivos
    db.add_task("Organizar notas fiscais")
    old = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
    with db.connection() as conn:
        conn.execute("UPDATE tasks SET created_at = ? WHERE title = ?", (old, "Organizar notas fiscais"))
    db._tasks_cache = None
    db.add_task("Tarefa nova de hoje")
    trig, sent, clock = make(db, datetime.now().timestamp(), env={"BYTE_AGENDA_ICS": ""})
    runner = objetivos.GoalRunner(objetivos.GoalStore(db), planner=lambda p: "1. a", executor=lambda i: "",
                                  notify=lambda t: None)
    trig.service.goal_runner = runner
    g = runner.create("Relatório do trimestre")
    runner.store.set_status(g["id"], "blocked")
    with db.connection() as conn:
        conn.execute("UPDATE goals SET updated_at = ? WHERE id = ?", (old, g["id"]))
    trig.tick()
    joined = "\n".join(sent)
    assert '"Organizar notas fiscais" está pendente há 8 dias' in joined and "Tarefa nova" not in joined
    assert f'objetivo #{g["id"]} "Relatório do trimestre" continua travado' in joined and f"/retomar {g['id']}" in joined
    clock["t"] += 7 * 3600
    assert trig.tick() == []                                                  # só volta a lembrar dias depois
    clock["t"] += 8 * 86400
    assert any("Organizar notas fiscais" in t for t in trig.tick())


def test_toggle_unconfigured_and_broken_watchers(db):
    def broken(url):
        raise OSError("sem rede")
    trig, sent, _ = make(db, ts(2026, 11, 20, 13, 40))
    trig.ics_fetcher = broken
    trig.tick()
    st = {t["name"]: t for t in trig.status()["triggers"]}
    assert "sem rede" in st["agenda"]["last_error"] and st["email"]["configured"] is False and sent == []
    trig.set_enabled("agenda", False)
    assert trig.enabled()["agenda"] is False and "agenda — compromisso chegando: desligado" in gatilhos.format_status(trig.status())
    with pytest.raises(ValueError):
        trig.set_enabled("inexistente", True)
    with pytest.raises(ValueError, match="https"):
        gatilhos.fetch_ics("http://exemplo.com/agenda.ics")


@pytest.fixture()
def fake():
    server = FakeOllama()
    yield server
    server.httpd.shutdown()
    engine.apply_profile("pessoal")


def test_installed_in_the_service_and_telegram(tmp_path, fake, monkeypatch):
    from byte_agent.telegram_bridge import TelegramBridge
    from test_telegram_bridge import OWNER, TOKEN, FakeTelegram
    monkeypatch.setenv("BYTE_WEB", "0")
    service = headless.build_service(tmp_path / "d", ollama_url=fake.url, model=TOOL_MODEL, profile="pessoal", web=False)
    tg = FakeTelegram()
    bridge = TelegramBridge(service, token=TOKEN, allowed_chat_ids=[OWNER], api_base=tg.url)
    try:
        assert service.trigger_engine and len(service.proactive_engine.background_jobs) == 2      # objetivos + gatilhos
        bridge.handle_update({"update_id": 1, "message": {"chat": {"id": OWNER}, "text": "/gatilhos"}})
        assert "tarefa_parada — tarefa parada: ligado" in tg.sent[-1]["text"] and "agenda" in tg.sent[-1]["text"]
        bridge.handle_update({"update_id": 2, "message": {"chat": {"id": OWNER}, "text": "/gatilho tarefa_parada off"}})
        assert tg.sent[-1]["text"] == "Gatilho tarefa_parada: desligado."
        assert service.trigger_engine.enabled()["tarefa_parada"] is False
        bridge.handle_update({"update_id": 3, "message": {"chat": {"id": OWNER}, "text": "/gatilho xyz on"}})
        assert "Gatilho desconhecido" in tg.sent[-1]["text"]
        assert "gatilhos" in service.dispatch_request("status", {})
    finally:
        bridge.stop()
        tg.httpd.shutdown()
        service.proactive_engine.stop()


def test_cloud_supervisor_sets_brasilia_time():
    from byte_agent import nuvem
    env = {}
    assert nuvem.set_timezone(env) == "America/Sao_Paulo" and env["TZ"] == "America/Sao_Paulo"
    assert nuvem.set_timezone({"TZ": "UTC"}) == "UTC"                         # quem já definiu, manda

"""v13.29 (Fase 3): a rotina — aprende casa, saídas e horários e sugere no momento certo."""
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlparse

import pytest

from byte_agent import engine, gatilhos, headless, rotina

TZ = gatilhos._tz()
HOME, WORK = (-22.7531, -47.4127), (-22.9056, -47.0608)          # Santa Bárbara d'Oeste e Campinas (~40 km)
MONDAY = datetime(2026, 11, 23, tzinfo=TZ)                         # segunda-feira em que as sugestões são testadas
ICS = """BEGIN:VCALENDAR
BEGIN:VEVENT
UID:dent
DTSTART;TZID=America/Sao_Paulo:20261123T140000
SUMMARY:Dentista
LOCATION:Rua Barão de Jaguara, 1000, Campinas
END:VEVENT
BEGIN:VEVENT
UID:reuniao
DTSTART;TZID=America/Sao_Paulo:20261123T090000
SUMMARY:Reunião
END:VEVENT
END:VCALENDAR
"""


class FakeNet:
    def __init__(self, rain=80):
        self.rain, self.calls = rain, []

    def __call__(self, url, timeout=10.0):
        self.calls.append(url)
        q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
        if "nominatim" in url and "/search" in url:
            return [{"lat": str(WORK[0]), "lon": str(WORK[1])}]
        if "nominatim" in url:
            return {"address": {"city": "Santa Bárbara d'Oeste", "suburb": "Centro"}}
        if "api.open-meteo.com" in url:
            return {"hourly": {"time": [f"2026-11-23T{h:02d}:00" for h in range(24)],
                               "precipitation_probability": [5] * 7 + [20, self.rain, 30] + [5] * 14}}
        raise AssertionError(url)


@pytest.fixture()
def svc(tmp_path, monkeypatch):
    from test_native_tools import TOOL_MODEL, FakeOllama
    monkeypatch.setenv("BYTE_AGENDA_ICS", "https://agenda.example/x.ics")
    fake = FakeOllama()
    service = headless.build_service(tmp_path, ollama_url=fake.url, model=TOOL_MODEL, profile="pessoal", web=False)
    clock = {"t": (MONDAY - timedelta(days=21)).timestamp()}
    net = FakeNet()
    service.local.getter, service.local.clock = net, lambda: clock["t"]
    rot = service.routine
    rot.clock, rot.getter, rot.ics_fetcher = (lambda: clock["t"]), net, (lambda url: ICS)
    rot.sent = []
    rot.notify = rot.sent.append
    service.test_clock, service.test_net = clock, net
    yield service
    service.proactive_engine.stop()
    fake.httpd.shutdown()
    engine.apply_profile("pessoal")


def at(svc, dt):
    svc.test_clock["t"] = dt.timestamp()


def live_three_weeks(svc):
    """Noites em casa; nos dias úteis sai por volta das 7h30 e fica no trabalho das 8h às 17h; fala com o agente das 8h às 21h."""
    for day in range(21):
        d = MONDAY - timedelta(days=21 - day)
        for h in range(0, 7):
            at(svc, d.replace(hour=h, minute=10))
            svc.extra_actions["location_update"]({"lat": HOME[0], "lon": HOME[1]})
        if d.weekday() < 5:
            at(svc, d.replace(hour=7, minute=10))
            svc.extra_actions["location_update"]({"lat": HOME[0], "lon": HOME[1]})
            at(svc, d.replace(hour=7, minute=45))
            svc.extra_actions["location_update"]({"lat": WORK[0], "lon": WORK[1]})
            for h in range(8, 18):
                at(svc, d.replace(hour=h, minute=20))
                svc.extra_actions["location_update"]({"lat": WORK[0], "lon": WORK[1]})
        for h in (8, 12, 18, 21):
            at(svc, d.replace(hour=h, minute=5))
            svc.proactive_engine.notify_user_activity()


def test_learns_home_work_leaving_time_and_active_hours(svc):
    live_three_weeks(svc)
    rot = svc.routine
    at(svc, MONDAY.replace(hour=6))
    assert rot.home() == rotina.cell_of(*HOME) and rot.work() == rotina.cell_of(*WORK)
    assert rot.leaving_hour(0) == 7.5 and rot.leaving_hour(5) is None           # sábado: sem padrão
    assert rot.active_window(0) == (8, 21)
    text = rot.describe()
    assert "🏠 Casa: identificada" in text and "seg 7h30" in text and "seg 8h–21h" in text
    with svc.db.connection() as conn:                                           # só contagens por área e hora, sem trajeto
        kinds = {r["kind"] for r in conn.execute("SELECT kind FROM rotina_sinais").fetchall()}
    assert kinds <= {"atividade", f"lugar:{rotina.cell_of(*HOME)}", f"lugar:{rotina.cell_of(*WORK)}", f"saida:{rotina.cell_of(*HOME)}"}


def test_rain_before_leaving_and_time_to_leave(svc):
    live_three_weeks(svc)
    rot = svc.routine
    at(svc, MONDAY.replace(hour=6, minute=50))
    svc.extra_actions["location_update"]({"lat": HOME[0], "lon": HOME[1]})
    sent = rot.tick()
    assert sent == ["☔ Você costuma sair por volta das 7h30 e há 80% de chance de chuva (por volta das 08:00). Leve o guarda-chuva!"]
    assert rot.tick() == []                                                     # uma vez por dia
    # dentista às 14h em Campinas, a ~40 km (≈57 min): avisa a partir de ~12h53
    at(svc, MONDAY.replace(hour=12, minute=45))
    svc.extra_actions["location_update"]({"lat": HOME[0], "lon": HOME[1]})
    assert rot.tick() == []
    at(svc, MONDAY.replace(hour=12, minute=55))
    svc.extra_actions["location_update"]({"lat": HOME[0], "lon": HOME[1]})
    sent = rot.tick()
    assert len(sent) == 1 and sent[0].startswith("🚗 Hora de sair: \"Dentista\" às 14:00") and "~40 km" in sent[0]
    at(svc, MONDAY.replace(hour=13, minute=40))
    late = rot.tick()
    assert late == [] or late[0].startswith("🚗 Já devia ter saído")             # o mesmo compromisso não repete o aviso


def test_repeated_reminders_become_a_weekly_suggestion(svc):
    db = svc.db
    with db.connection() as conn:
        for w in (3, 2, 1):
            due = (MONDAY - timedelta(weeks=w)).replace(hour=9, minute=0).timestamp()
            conn.execute("INSERT INTO reminders (text, due_epoch, repeat_s, kind, status, created_at) VALUES (?, ?, 0, 'note', 'done', ?)",
                         ("pagar a academia", due, db.now_iso()))
    rot = svc.routine
    at(svc, MONDAY.replace(hour=10))
    sent = rot.tick()
    assert any(t.startswith("🔁 Nas últimas 3 semanas, às segundas por volta das 9h") and "/rotina aceitar 1" in t for t in sent)
    out = svc.extra_actions["routine_command"]({"arg": "aceitar 1"})["text"]
    assert out.startswith("✅ Lembrete fixo criado: \"pagar a academia\" toda segunda às 09:00")
    weekly = [r for r in db.list_reminders() if r["repeat"] == "weekly"]
    assert len(weekly) == 1 and datetime.fromtimestamp(weekly[0]["due_epoch"], TZ).weekday() == 0
    assert rot.repeated_reminders() == []                                       # virou rotina: não sugere de novo


def test_quiet_hours_cap_and_off(svc):
    live_three_weeks(svc)
    rot = svc.routine
    with svc.db.connection() as conn:
        for w in (3, 2, 1):
            conn.execute("INSERT INTO reminders (text, due_epoch, repeat_s, kind, status, created_at) VALUES (?, ?, 0, 'note', 'done', ?)",
                         ("regar as plantas", (MONDAY - timedelta(weeks=w)).replace(hour=19).timestamp(), svc.db.now_iso()))
    at(svc, MONDAY.replace(hour=3))
    assert rot.tick() == []                                                     # de madrugada, só o "hora de sair"
    svc.extra_actions["routine_command"]({"arg": "off"})
    at(svc, MONDAY.replace(hour=19))
    assert rot.tick() == []
    svc.extra_actions["routine_command"]({"arg": "on"})
    assert rot.tick()
    svc.extra_actions["routine_command"]({"arg": "esquecer"})
    assert rot.home() is None and "ainda não" in rot.describe()


def test_morning_brief_gets_tips(svc):
    live_three_weeks(svc)
    svc.db.add_task(title="Organizar a garagem", priority=5)
    at(svc, MONDAY.replace(hour=6, minute=55))
    svc.extra_actions["location_update"]({"lat": HOME[0], "lon": HOME[1]})
    lines = svc.routine.briefing_lines(svc.routine._local())
    assert lines[0].startswith("☔ Chuva (80%) perto da hora em que você costuma sair (7h30)")
    assert any(t.startswith("💡 Você tem um horário livre das 10:00 às 14:00") and "Organizar a garagem" in t for t in lines)
    from byte_agent import resumos
    b = resumos.Briefings(svc, clock=svc.routine.clock, ics_fetcher=lambda url: ICS, writer=lambda k, f: "",
                          weather=lambda city, db: None)
    assert "🧭 Para hoje:\n☔ Chuva (80%)" in b.morning()["text"]


def test_telegram_rotina(svc):
    from byte_agent.telegram_bridge import TelegramBridge
    from test_telegram_bridge import OWNER, TOKEN, FakeTelegram
    tg = FakeTelegram()
    bridge = TelegramBridge(svc, token=TOKEN, allowed_chat_ids=[OWNER], api_base=tg.url)
    try:
        bridge.handle_update({"update_id": 1, "message": {"chat": {"id": OWNER}, "text": "/rotina"}})
        assert "🧭 O que aprendi da sua rotina" in tg.sent[-1]["text"]
        bridge.handle_update({"update_id": 2, "message": {"chat": {"id": OWNER}, "text": "/rotina aceitar 9"}})
        assert "Sugestão não encontrada" in tg.sent[-1]["text"]
    finally:
        bridge.stop()
        tg.httpd.shutdown()

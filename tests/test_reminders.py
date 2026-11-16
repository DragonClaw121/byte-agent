"""Autonomia por horário: lembretes, rotinas e tarefas agendadas executadas pela LLM."""
import time
from datetime import datetime, timedelta

import pytest

from byte_agent import engine, headless
from byte_agent.telegram_bridge import TelegramBridge
from test_native_tools import PLAIN_MODEL, TOOL_MODEL, FakeOllama, call
from test_telegram_bridge import OWNER, TOKEN, FakeTelegram

NOW = datetime(2026, 11, 14, 15, 0)


@pytest.mark.parametrize("text, expected, rest", [
    ("30m beber água", NOW + timedelta(minutes=30), "beber água"),
    ("em 2 horas ligar para a Ana", NOW + timedelta(hours=2), "ligar para a Ana"),
    ("1d pagar boleto", NOW + timedelta(days=1), "pagar boleto"),
    ("16:30 reunião", datetime(2026, 11, 14, 16, 30), "reunião"),
    ("09:00 academia", datetime(2026, 11, 15, 9, 0), "academia"),                 # já passou hoje -> amanhã
    ("amanhã 08:15 dentista", datetime(2026, 11, 15, 8, 15), "dentista"),
    ("amanha às 8h15 dentista", datetime(2026, 11, 15, 8, 15), "dentista"),
    ("2026-12-01 14:00 prova", datetime(2026, 12, 1, 14, 0), "prova"),
    ("2026-12-01T14:00", datetime(2026, 12, 1, 14, 0), ""),
    ("25/12 08:00 natal", datetime(2026, 12, 25, 8, 0), "natal"),
    ("01/02 10:00 volta", datetime(2027, 2, 1, 10, 0), "volta"),                   # data já passou este ano -> ano que vem
    ("25/12/2027 08:00 x", datetime(2027, 12, 25, 8, 0), "x"),
])
def test_parse_when(text, expected, rest):
    assert engine.parse_when(text, NOW) == (expected, rest)


@pytest.mark.parametrize("bad", ["", "sem horário nenhum", "25:99 x", "amanhã cedo"])
def test_parse_when_rejects_garbage(bad):
    with pytest.raises(ValueError):
        engine.parse_when(bad, NOW)


@pytest.fixture()
def env(tmp_path):
    fake = FakeOllama()
    service = headless.build_service(tmp_path, ollama_url=fake.url, model=TOOL_MODEL)
    service.proactive_engine.stop()                       # os testes disparam o agendador manualmente
    service.proactive_engine.autonomous_mode_enabled = False
    service.llm.auto_effort_routing = False
    yield fake, service
    service.shutdown()
    fake.httpd.shutdown()


def outbox(service):
    return [m["message"] for m in service.db.pop_undelivered_proactive_messages()]


def test_one_shot_reminder_fires_once_at_its_time(env):
    _, service = env
    now = time.time()
    rem = service.db.add_reminder("beber água", now + 600)
    engine_ = service.proactive_engine
    assert engine_.process_due_reminders(now + 599) == 0 and outbox(service) == []
    assert engine_.process_due_reminders(now + 601) == 1
    assert outbox(service) == ["⏰ Lembrete: beber água"]
    assert engine_.process_due_reminders(now + 5000) == 0                          # não repete
    assert service.db.list_reminders() == [] and service.db.list_reminders(include_done=True)[0]["id"] == rem["id"]


def test_reminders_fire_even_with_initiatives_disabled_and_report_lateness(env):
    _, service = env
    now = time.time()
    service.db.add_reminder("tomar remédio", now + 60)
    assert service.proactive_engine.autonomous_mode_enabled is False
    service.proactive_engine.process_due_reminders(now + 3 * 3600)                 # máquina ficou desligada (dual-boot)
    message = outbox(service)[0]
    assert message.startswith("⏰ Lembrete: tomar remédio (atrasado: era para ") and "desligada" in message


def test_daily_routine_advances_to_the_next_future_slot(env):
    _, service = env
    now = time.time()
    rem = service.db.add_reminder("bom dia", now + 100, repeat="daily")
    service.proactive_engine.process_due_reminders(now + 100 + 3 * 86400 + 5)      # 3 dias desligada
    assert len(outbox(service)) == 1                                               # dispara uma vez, não três
    pending = service.db.list_reminders()
    assert len(pending) == 1 and abs(pending[0]["due_epoch"] - (now + 100 + 4 * 86400)) < 1
    assert pending[0]["repeat"] == "daily" and pending[0]["id"] == rem["id"]


def test_validation_and_cancel(env):
    _, service = env
    with pytest.raises(ValueError):
        service.db.add_reminder("x", time.time() - 3600)
    with pytest.raises(ValueError):
        service.db.add_reminder("texto", time.time() + 10 * 366 * 86400)
    with pytest.raises(ValueError):
        service.create_reminder("sem horário")
    rem = service.create_reminder("ligar", in_minutes=5)
    assert service.db.cancel_reminder(rem["id"]) is True and service.db.cancel_reminder(rem["id"]) is False
    assert service.proactive_engine.process_due_reminders(time.time() + 9999) == 0


def test_llm_schedules_a_reminder_with_the_native_tool(env):
    fake, service = env
    when = (datetime.now() + timedelta(hours=2)).replace(second=0, microsecond=0)
    fake.script = [call("schedule_reminder", text="Ligar para o dentista", when=when.isoformat(timespec="minutes")),
                   {"content": "Combinado, te lembro em duas horas."}]
    res = service.interact("me lembra de ligar para o dentista daqui a 2 horas")
    assert [t["tool"] for t in res["tool_trace"]] == ["schedule_reminder"]
    pending = service.db.list_reminders()
    assert len(pending) == 1 and pending[0]["text"] == "Ligar para o dentista" and pending[0]["kind"] == "note"
    assert abs(pending[0]["due_epoch"] - when.timestamp()) < 1
    assert '"agendado": true' in fake.tool_rounds()[1]["messages"][-1]["content"]
    fake.script = [call("schedule_reminder", text="x", when="ontem de manhã"), {"content": "Não consegui entender o horário."}]
    service.interact("agenda um negócio aí")
    assert fake.tool_rounds()[-1]["messages"][-1]["content"].startswith("ERRO:")   # o erro volta para o modelo, sem exceção
    assert len(service.db.list_reminders()) == 1


def run_missions(service, now):
    count = service.proactive_engine.process_due_reminders(now)
    deadline = time.time() + 10
    messages = []
    while time.time() < deadline and len(messages) < count:
        messages += outbox(service)
        time.sleep(0.05)
    return messages


def test_scheduled_task_runs_unattended_with_read_only_tools(env):
    fake, service = env
    service.db.add_task("Validar arquitetura do EVFI", priority=5, scheduled_slot="hoje 16h")
    now = time.time()
    service.db.add_reminder("Resuma minha agenda de hoje", now + 60, kind="mission", repeat="daily")
    fake.script = [call("list_tasks"), {"content": "Hoje às 16h: validar a arquitetura do EVFI."}]
    messages = run_missions(service, now + 61)
    assert len(messages) == 1 and messages[0].startswith("🗓️ Tarefa agendada #1 — Resuma minha agenda de hoje")
    assert "validar a arquitetura do EVFI" in messages[0]
    first = fake.tool_rounds()[0]
    offered = {t["function"]["name"] for t in first["tools"]}
    assert offered == set(engine.ByteEcosystemService.MISSION_TOOLS)                # leitura/agenda/memória apenas
    assert not offered & {"run_python", "run_shell", "email_send", "email_inbox", "schedule_reminder", "cancel_reminder"}
    assert "não faça perguntas" in first["messages"][0]["content"]
    assert len(service.db.list_reminders()) == 1                                    # a rotina continua agendada


def test_scheduled_task_falls_back_to_text_mode_and_reports_failures(env):
    fake, service = env
    service.llm.active_model = PLAIN_MODEL
    service.db.add_task("Comprar pão", priority=3)
    now = time.time()
    service.db.add_reminder("O que tenho pendente?", now + 60, kind="mission")
    fake.stream_reply = "Você tem pendente: comprar pão."
    messages = run_missions(service, now + 61)
    assert "comprar pão" in messages[0] and fake.tool_rounds() == []
    assert "Comprar pão" in fake.requests[-1]["messages"][-1]["content"]            # agenda injetada no prompt
    service.proactive_engine.mission_runner = lambda text: (_ for _ in ()).throw(RuntimeError("GPU ocupada"))
    service.db.add_reminder("outra", now + 60, kind="mission")
    messages = run_missions(service, now + 61)
    assert "Não consegui executar agora" in messages[0] and "GPU ocupada" in messages[0]


def test_panel_dispatch_and_status(env):
    _, service = env
    res = service.dispatch_request("add_reminder", {"spec": "45m tirar o bolo do forno"})
    assert res["text"] == "tirar o bolo do forno" and res["kind"] == "note"
    res = service.dispatch_request("add_reminder", {"spec": "08:00 resuma minha agenda", "repeat": "daily", "run_as_task": True})
    assert res["kind"] == "mission" and res["repeat"] == "daily"
    assert "error" in service.dispatch_request("add_reminder", {"spec": "qualquer hora dessas"})
    listed = service.dispatch_request("status", {})["reminders"]
    assert [r["text"] for r in listed] == sorted([r["text"] for r in listed], key=lambda t: [x["text"] for x in listed].index(t))
    assert len(listed) == 2
    assert service.dispatch_request("cancel_reminder", {"reminder_id": listed[0]["id"]})["cancelled"] is True


def test_telegram_commands_and_delivery(env):
    _, service = env
    tg = FakeTelegram()
    bridge = TelegramBridge(service, TOKEN, [OWNER], api_base=tg.url, poll_timeout_s=0)
    tg.text(1, OWNER, "/lembrete 30m beber água")
    tg.text(2, OWNER, "/rotina 08:00 resuma minha agenda de hoje")
    tg.text(3, OWNER, "/lembretes")
    tg.text(4, OWNER, "/lembrete semana que vem algo")
    bridge.poll_once()
    texts = [m["text"] for m in tg.sent]
    assert texts[0].startswith("⏰ Lembrete #1 para ") and texts[0].endswith("beber água")
    assert texts[1].startswith("🗓️ Rotina diária #2 para ") and "(todo dia)" in texts[2] and "#1 ⏰" in texts[2]
    assert "Não entendi o horário" in texts[3]
    service.proactive_engine.process_due_reminders(time.time() + 31 * 60)
    assert bridge.push_proactive() == 1 and tg.sent[-1]["text"] == "🤖 ⏰ Lembrete: beber água"
    tg.text(5, OWNER, "/cancelar 2")
    tg.text(6, OWNER, "/live")
    bridge.poll_once()
    assert tg.sent[-2]["text"] == "Cancelado." and "só roda no perfil VTuber" in tg.sent[-1]["text"]
    tg.httpd.shutdown()


def test_telegram_live_kill_switch(env):
    _, service = env
    from byte_agent.stream import StreamConfig, StreamDirector
    tg = FakeTelegram()
    bridge = TelegramBridge(service, TOKEN, [OWNER], api_base=tg.url, poll_timeout_s=0)
    bridge.stream_director = StreamDirector(service.llm, StreamConfig())
    tg.text(1, OWNER, "/live off")
    bridge.poll_once()
    assert bridge.stream_director.paused is True and "PAUSADA" in tg.sent[-1]["text"]
    tg.text(2, OWNER, "/live on")
    bridge.poll_once()
    assert bridge.stream_director.paused is False and "ativa" in tg.sent[-1]["text"]
    tg.httpd.shutdown()

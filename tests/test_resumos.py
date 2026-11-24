"""v13.24 (Fase 2.3): resumo da manhã, fechamento da semana e tarefas concluídas."""
from datetime import datetime, timedelta

import time
import pytest

from byte_agent import engine, gatilhos, headless, permissoes, resumos
from test_native_tools import TOOL_MODEL, FakeOllama

TZ = gatilhos._tz()
NOW = datetime(2026, 11, 19, 7, 10, tzinfo=TZ)            # quinta-feira, 07:10
ICS = """BEGIN:VCALENDAR
BEGIN:VEVENT
UID:a1
DTSTART;TZID=America/Sao_Paulo:20261119T090000
SUMMARY:Reunião com o time
LOCATION:Sala 2
END:VEVENT
BEGIN:VEVENT
UID:a2
DTSTART;TZID=America/Sao_Paulo:20261119T143000
SUMMARY:Dentista
END:VEVENT
BEGIN:VEVENT
UID:a3
DTSTART;VALUE=DATE:20261119
SUMMARY:Aniversário da Ana
END:VEVENT
BEGIN:VEVENT
UID:a4
DTSTART;TZID=America/Sao_Paulo:20261124T100000
SUMMARY:Entrega do relatório
END:VEVENT
END:VCALENDAR
"""


@pytest.fixture()
def fake():
    server = FakeOllama()
    yield server
    server.httpd.shutdown()
    engine.apply_profile("pessoal")


@pytest.fixture()
def service(tmp_path, fake, monkeypatch):
    monkeypatch.setenv("BYTE_WEB", "0")
    monkeypatch.setenv("BYTE_AGENDA_ICS", "https://agenda.example/secreto.ics")
    monkeypatch.delenv("BYTE_RESUMO_CIDADE", raising=False)
    svc = headless.build_service(tmp_path / "d", ollama_url=fake.url, model=TOOL_MODEL, profile="pessoal", web=False)
    yield svc
    svc.proactive_engine.stop()


def briefings(svc, now=NOW, writer=lambda kind, facts: "", weather=None):
    clock = {"t": now.timestamp()}
    b = resumos.Briefings(svc, clock=lambda: clock["t"], ics_fetcher=lambda url: ICS, writer=writer,
                          weather=weather or (lambda city, db: {"cidade": city, "min": 18, "max": 27, "chuva": 60, "ceu": "nublado"}))
    b.clock_state = clock
    return b


def test_morning_brief_collects_everything_without_inventing(service):
    db = service.db
    db.add_task(title="Pagar a conta de luz", priority=5)
    db.add_task(title="Comprar ração", priority=2)
    with db.connection() as conn:                                  # direto no banco: add_reminder recusa horário passado
        conn.execute("INSERT INTO reminders (text, due_epoch, repeat_s, kind, status, created_at) VALUES (?, ?, 0, 'note', "
                     "'pending', ?)", ("tomar o remédio", NOW.replace(hour=12).timestamp(), db.now_iso()))
    resumos.save_config(db, "outro", {"cidade": "São Paulo"})
    b = briefings(service, writer=lambda kind, facts: "Bom dia, chefe! Dia cheio." if "Dentista" in facts else "")
    res = b.morning()
    text = res["text"]
    assert text.startswith("☀️ Quinta, 19/11\nBom dia, chefe! Dia cheio.")
    assert "🌤️ São Paulo: 18–27 °C, nublado, chuva 60%" in text
    assert "📅 Hoje (3):\n• dia todo Aniversário da Ana\n• 09:00 Reunião com o time — Sala 2\n• 14:30 Dentista" in text
    assert "Entrega do relatório" not in text                      # é da semana que vem
    assert "⏰ Lembretes de hoje (1):" in text and "tomar o remédio" in text
    assert "📌 Tarefas pendentes (2):\n• #1 Pagar a conta de luz" in text
    assert "às 9h, Reunião com o time" in res["speech"] and "às 14h30, Dentista" in res["speech"]
    assert not res["third_party"]


def test_morning_brief_without_llm_or_calendar(service, monkeypatch):
    monkeypatch.delenv("BYTE_AGENDA_ICS")
    b = briefings(service, writer=lambda kind, facts: (_ for _ in ()).throw(RuntimeError("sem LLM")))
    res = b.morning()
    assert "Bom dia! Aqui vai o seu quinta." in res["text"] and "Dia livre" in res["text"]
    assert b.last_error.startswith("abertura: RuntimeError")


def test_defaults_per_agent_and_schedule_once_per_day(service):
    assert resumos.agent_kind(service) == "outro"
    b = briefings(service)
    assert b.due() == []                                          # Matrix/Byte: desligado por padrão
    service.agent_manifest = {"id": "kronus", "tipo": "assistente", "habilidades": ["agenda"]}
    assert b.kind == "kronus" and b.config()["manha"]["hora"] == "07:00"
    assert b.due() == ["manha"]
    sent = b.tick()
    assert len(sent) == 1 and b.tick() == []                      # uma vez por dia
    msgs = service.db.pop_undelivered_proactive_messages("app")
    assert msgs[-1]["trigger_type"] == "briefing" and msgs[-1]["message"].startswith("☀️ Quinta")
    spoken = service.db.get_setting(resumos.SPEECH)[str(msgs[-1]["id"])]
    assert "☀" not in spoken and "Reunião com o time" in spoken
    # tarde demais (nuvem ligou às 11h): não manda o da manhã atrasado
    late = briefings(service, now=NOW.replace(day=20, hour=11))
    assert late.due() == []
    # Bit: dias úteis às 08:00; sábado não
    service.agent_manifest = {"id": "bit", "tipo": "trabalho", "habilidades": []}
    assert briefings(service, now=NOW.replace(day=21, hour=8, minute=5)).due() == []
    assert briefings(service, now=NOW.replace(day=20, hour=8, minute=5)).due() == ["manha"]


def test_weekly_review_with_completed_tasks(service):
    db = service.db
    t1 = db.add_task(title="Pagar a conta de luz", priority=5)
    db.add_task(title="Organizar a garagem", priority=2)
    assert db.complete_task(t1["id"])["title"] == "Pagar a conta de luz"
    assert db.complete_task(t1["id"]) is None                      # já concluída
    assert [t["title"] for t in db.list_tasks(status="pending")] == ["Organizar a garagem"]
    b = briefings(service, now=NOW.replace(day=22, hour=19, minute=5))      # domingo
    res = b.weekly()
    assert "✅ Concluídas (1):\n• Pagar a conta de luz" in res["text"]
    assert "📌 Ficou para a próxima (1 tarefa):" in res["text"] and "Organizar a garagem" in res["text"]
    assert "📅 Próximos 7 dias (1):\n• ter 24/11 10:00 Entrega do relatório" in res["text"]
    assert "concluiu 1 tarefa" in res["speech"]
    service.agent_manifest = {"id": "kronus", "tipo": "assistente", "habilidades": []}
    assert "semana" in b.due()


def test_commands_and_validation(service):
    b = briefings(service)
    assert "Resumo da manhã: ligado — 06:45" in b.command("manha", "hora 6:45")
    assert "segunda a sexta" in b.command("manha", "dias úteis")
    assert "Fechamento da semana: ligado — sexta, 18:30" in b.command("semana", "sexta 18:30")
    assert "Fechamento da semana: desligado" in b.command("semana", "off")
    assert "Dia inválido" in b.command("semana", "feriado 10:00")
    with pytest.raises(ValueError):
        resumos.save_config(service.db, "outro", {"manha": {"hora": "25:00"}})
    assert "Notícias: IA, games" in b.command("manha", "noticias IA, games")
    assert "nenhum tema" in b.command("manha", "noticias off")


def test_complete_task_tool_respects_permissions(service):
    service.db.add_task(title="Levar o carro na revisão", priority=3)
    turn = {"blocks": [], "executions": [], "wrote": False}
    out = service.run_agent_tool("complete_task", {"title": "carro revisão"}, turn)
    assert '"concluida": true' in out and service.db.list_tasks(status="pending") == []
    service.db.add_task(title="Comprar pão", priority=3)
    permissoes.set_level(service.db, "tarefas", "sugerir")
    out = service.run_agent_tool("complete_task", {"title": "pão"}, {"blocks": [], "executions": [], "wrote": False})
    assert out.startswith("SÓ SUGERIR") and len(service.db.list_tasks(status="pending")) == 1
    assert "Marcar como concluída a tarefa \"pão\"" in permissoes.format_activity(permissoes.activity(service.db))


def test_brief_tool_and_telegram(service):
    out = service.run_agent_tool("resumo_do_dia", {"periodo": "dia"}, {"blocks": [], "executions": [], "wrote": False})
    assert out.startswith("DADOS DO RESUMO DO DIA")
    from byte_agent.telegram_bridge import TelegramBridge
    from test_telegram_bridge import OWNER, TOKEN, FakeTelegram
    tg = FakeTelegram()
    bridge = TelegramBridge(service, token=TOKEN, allowed_chat_ids=[OWNER], api_base=tg.url)
    service.db.add_task(title="Ligar para o banco", priority=3)
    try:
        def cmd(text, n=[0]):
            n[0] += 1
            bridge.handle_update({"update_id": n[0], "message": {"chat": {"id": OWNER}, "text": text}})
            return tg.sent[-1]["text"]
        assert "Resumos automáticos" in cmd("/resumo config")
        assert "07:15" in cmd("/resumo hora 07:15")
        assert "#1 [P3] Ligar para o banco" in cmd("/agenda")
        assert "✅ Concluída: Ligar para o banco" in cmd("/feito 1")
        assert "Não achei" in cmd("/feito 1")
        assert "Semana de" in cmd("/semana")
    finally:
        bridge.stop()
        tg.httpd.shutdown()


def test_weather_parsing(service):
    calls = []

    def getter(url):
        calls.append(url)
        if "geocoding" in url:
            return {"results": [{"name": "Curitiba", "latitude": -25.4, "longitude": -49.3}]}
        return {"daily": {"weather_code": [61], "temperature_2m_max": [24.6], "temperature_2m_min": [13.2],
                          "precipitation_probability_max": [80]}}
    w = resumos.fetch_weather("Curitiba", service.db, getter=getter)
    assert w == {"cidade": "Curitiba", "min": 13, "max": 25, "chuva": 80, "ceu": "chuva fraca"}
    resumos.fetch_weather("Curitiba", service.db, getter=getter)
    assert sum("geocoding" in c for c in calls) == 1              # a localização fica guardada


def test_news_uses_news_search_and_failures_are_visible(service):
    from byte_agent import web as webmod

    class FakeTavily(webmod.SearchBackend):
        name, news = "Tavily", True

        def __init__(self):
            self.calls = []

        def search(self, fetcher, query, limit, news=False):
            self.calls.append((query, news))
            return [webmod.SearchHit("OpenAI lança modelo novo", "https://www.g1.globo.com/tec/1", "", self.name)]

    tav = FakeTavily()
    service.web = webmod.WebAccess(service, ".", backends=[tav, webmod.WikipediaBackend()])
    resumos.save_config(service.db, "outro", {"noticias": "IA", "cidade": "Cidadeinexistente"})

    def no_city(city, db):
        raise ValueError("cidade não encontrada: " + city)
    res = briefings(service, weather=no_city).morning(with_opening=False)
    assert "📰 Manchetes:\n• [IA] OpenAI lança modelo novo (g1.globo.com)" in res["text"]
    assert tav.calls == [("IA", True)] and res["third_party"]
    assert "Não consegui ver o clima de Cidadeinexistente: cidade não encontrada" in res["text"]
    # só a Wikipédia (sem Tavily): não traz verbetes como se fossem manchetes, e diz o porquê
    service.web = webmod.WebAccess(service, ".", backends=[webmod.WikipediaBackend()])
    b = briefings(service)
    text = b.morning(with_opening=False)["text"]
    assert "Não consegui buscar as manchetes: nenhum buscador de notícias configurado" in text
    assert "⚠️ Último problema: notícias" in b.command("manha", "config")


def test_morning_brief_lists_client_next_steps(service, monkeypatch):
    """v13.63: os próximos passos das fichas de clientes (mexidas nas últimas 3 semanas) entram no resumo da manhã."""
    monkeypatch.delenv("BYTE_AGENDA_ICS")
    service.client_book.upsert("EMS S/A", proximos=["Enviar proposta (Gabriel) até sexta", "Agendar onboarding"])
    service.client_book.upsert("Sem passos Ltda")
    b = briefings(service, writer=lambda kind, facts: "")
    b.clock = time.time
    text = b.morning()["text"]
    assert "🗂️ Clientes — próximos passos:\n• EMS S/A: Enviar proposta (Gabriel) até sexta; Agendar onboarding" in text
    assert "Sem passos" not in text and "Dia livre" not in text

"""v13.23 (Fase 1.3): níveis de permissão por tipo de ação e registro de atividade."""
import time

import pytest

from byte_agent import engine, headless, permissoes
from test_native_tools import TOOL_MODEL, FakeOllama


@pytest.fixture()
def fake():
    server = FakeOllama()
    yield server
    server.httpd.shutdown()
    engine.apply_profile("pessoal")


@pytest.fixture()
def service(tmp_path, fake, monkeypatch):
    monkeypatch.setenv("BYTE_WEB", "0")
    svc = headless.build_service(tmp_path / "d", ollama_url=fake.url, model=TOOL_MODEL, profile="pessoal", web=False)
    yield svc
    svc.proactive_engine.stop()


def new_turn(**extra):
    return dict({"blocks": [], "executions": [], "wrote": False}, **extra)


def titles(svc):
    svc.db._tasks_cache = None
    return [t["title"] for t in svc.db.list_tasks(status="pending")]


def test_levels_defaults_and_floor(tmp_path):
    db = engine.UnifiedDatabase(tmp_path / "b.db", engine.VectorEngine())
    lv = permissoes.levels(db)
    assert lv["tarefas"] == "automatico" and lv["email"] == "aprovar" and lv["codigo"] == "aprovar"
    assert permissoes.set_level(db, "tarefas", "sugerir")["tarefas"] == "sugerir"
    with pytest.raises(PermissionError, match="máximo é 'aprovar'"):
        permissoes.set_level(db, "email", "automatico")
    with pytest.raises(ValueError):
        permissoes.set_level(db, "xyz", "aprovar")
    db.set_setting("permissoes", {"integracoes": "automatico"})          # valor antigo/forjado no banco
    assert permissoes.levels(db)["integracoes"] == "aprovar"              # o piso vale mesmo assim
    permissoes.set_level(db, "tarefas", "sugerir")
    assert "Tarefas da agenda: só sugerir" in permissoes.format_levels(permissoes.levels(db))


def test_automatic_suggest_and_approve_paths(service):
    db = service.db
    # automático: faz e registra
    out = service.run_agent_tool("add_task", {"title": "Pagar a conta de luz"}, new_turn())
    assert "Pagar a conta de luz" in titles(service) and "SÓ SUGERIR" not in out
    # sugerir: nada é feito
    permissoes.set_level(db, "tarefas", "sugerir")
    out = service.run_agent_tool("add_task", {"title": "Comprar pão"}, new_turn())
    assert out.startswith("SÓ SUGERIR") and "Comprar pão" not in titles(service)
    # aprovar: cartão; só depois de aprovado
    permissoes.set_level(db, "tarefas", "aprovar")
    turn = new_turn()
    out = service.run_agent_tool("add_task", {"title": "Levar o carro na revisão", "scheduled_slot": "sábado"}, turn)
    assert out.startswith("AGUARDANDO APROVAÇÃO") and "Levar o carro" not in " ".join(titles(service))
    block = turn["blocks"][0]
    assert block.startswith("```byte-action") and "Criar a tarefa" in block
    import json
    card = json.loads(block.split("\n", 1)[1].rsplit("```", 1)[0])
    service.approve_pending_action(card["id"], {"title": card["title"], "details": card["details"]})
    assert "Levar o carro na revisão" in titles(service)
    # registro de atividade
    text = permissoes.format_activity(permissoes.activity(db))
    assert "🤖 fez: Criar a tarefa \"Pagar a conta de luz\"" in text and "💡 sugeriu: Criar a tarefa \"Comprar pão\"" in text
    assert "✅ aprovado" in text and "🔐 pediu aprovação" in text


def test_unattended_approval_card_goes_to_telegram_and_panel(service):
    permissoes.set_level(service.db, "lembretes", "aprovar")
    out = service.run_agent_tool("schedule_reminder", {"text": "beber água", "in_minutes": 30}, new_turn(unattended=True))
    assert out.startswith("AGUARDANDO APROVAÇÃO")
    msgs = [m["message"] for m in service.db.pop_undelivered_proactive_messages("telegram")]
    assert any(m.startswith("🔐 Preciso da sua aprovação: Agendar o lembrete \"beber água\"") and "byte-action" in m for m in msgs)
    assert service.db.list_reminders() == []


def test_external_actions_keep_their_own_approval_and_suggest_works(service):
    permissoes.set_level(service.db, "email", "sugerir")
    out = service.run_agent_tool("email_send", {"to": "a@b.com", "subject": "Oi", "body": "Teste"}, new_turn())
    assert out.startswith("SÓ SUGERIR") and not service.pending_actions


def test_background_extraction_respects_levels(service, monkeypatch):
    permissoes.set_level(service.db, "tarefas", "sugerir")
    permissoes.set_level(service.db, "memorias", "aprovar")
    monkeypatch.setattr(service.llm, "extract_agenda_and_memories_via_llm", lambda msg: {
        "tasks": [{"title": "Dentista", "category": "saude", "priority": 3, "notes": "", "scheduled_slot": "sexta 15h"}],
        "memories": [{"content": "Prefere atendimento de manhã", "category": "perfil", "importance": 3}]})
    monkeypatch.setattr(engine.ContextGatekeeper, "has_potential_commitment_or_memory", staticmethod(lambda msg: True))
    service._extract_agenda_in_background_async("tenho dentista sexta às 15h e prefiro de manhã")
    msgs = []
    for _ in range(40):
        msgs += [m["message"] for m in service.db.pop_undelivered_proactive_messages("telegram")]
        if msgs:
            break
        time.sleep(0.1)
    joined = "\n".join(msgs)
    assert "💡 Sugestão: Criar a tarefa \"Dentista\"" in joined and "🔐 Posso? Guardar na memória" in joined
    assert "Dentista" not in titles(service)
    service.db._vector_cache = None
    assert not any("manhã" in m["content"] for m in service.db.list_memories())


def test_telegram_commands(service):
    from byte_agent.telegram_bridge import TelegramBridge
    from test_telegram_bridge import OWNER, TOKEN, FakeTelegram
    tg = FakeTelegram()
    bridge = TelegramBridge(service, token=TOKEN, allowed_chat_ids=[OWNER], api_base=tg.url)
    try:
        def cmd(text, n=[0]):
            n[0] += 1
            bridge.handle_update({"update_id": n[0], "message": {"chat": {"id": OWNER}, "text": text}})
            return tg.sent[-1]["text"]
        assert "Tarefas da agenda: fazer e registrar" in cmd("/permissoes")
        assert "Tarefas da agenda: pedir aprovação" in cmd("/permissao tarefas aprovar")
        assert "máximo é 'aprovar'" in cmd("/permissao email automatico")
        assert "Use: /permissao" in cmd("/permissao tarefas")
        assert "O que a Byte fez" in cmd("/atividade") or "Nenhuma ação" in tg.sent[-1]["text"]
    finally:
        bridge.stop()
        tg.httpd.shutdown()

"""v13.9: objetivos de longo prazo — planejar, executar uma etapa por vez, travar com aviso, orçamento diário."""
import argparse
import json

import pytest

from byte_agent import cloud, engine, headless, objetivos
from test_native_tools import TOOL_MODEL, FakeOllama


def make_runner(tmp_path, plan="1. Buscar cursos\n2. Comparar preços\n3. Entregar o comparativo final", results=None,
                tokens=0, budget=None):
    db = engine.UnifiedDatabase(tmp_path / "byte.db", engine.VectorEngine())
    sent, prompts = [], []
    results = list(results or [])
    clock = {"t": 1000.0}

    def executor(instruction):
        prompts.append(instruction)
        nxt = results.pop(0) if results else "Resultado detalhado da etapa, com dados e links úteis."
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    runner = objetivos.GoalRunner(objetivos.GoalStore(db), planner=lambda p: plan, executor=executor, notify=sent.append,
                                  tokens_today=lambda: tokens, daily_budget=budget, clock=lambda: clock["t"])
    return runner, sent, prompts, clock


def advance(runner, clock, times=1, step=4000.0):
    out = []
    for _ in range(times):
        out.append(runner.tick())
        clock["t"] += step
    return out


def test_plan_parsing_always_ends_with_a_delivery_step():
    assert objetivos.parse_steps("1. Pesquisar\n2) Resumir os achados", "x")[-1] == "Resumir os achados"
    steps = objetivos.parse_steps("- Buscar dados\n- Comparar", "x")
    assert steps[:2] == ["Buscar dados", "Comparar"] and "resultado final" in steps[-1]
    assert objetivos.parse_steps("Não sei planejar.", "Resumo de IA")[0] == "Resumo de IA"         # sem lista
    many = "\n".join(f"{i}. Etapa número {i}" for i in range(1, 12))
    assert len(objetivos.parse_steps(many, "x")) == objetivos.MAX_STEPS


def test_goal_runs_one_step_at_a_time_carries_results_and_delivers(tmp_path):
    runner, sent, prompts, clock = make_runner(tmp_path, results=[
        "Achei 3 cursos: A (grátis), B (R$ 50) e C (R$ 120), com links.",
        "Comparação: A é o melhor custo-benefício; B tem certificado.",
        "Comparativo final: recomendo A para começar e B se precisar de certificado."])
    goal = runner.create("Comparar cursos de Python", "gratuitos de preferência")
    assert [s["text"] for s in goal["steps"]][0] == "Buscar cursos" and len(goal["steps"]) == 3
    assert runner.tick()["status"] == "done" and runner.store.get(goal["id"])["done_steps"] == 1
    assert runner.tick() is None                                       # respeita o intervalo entre etapas
    clock["t"] += objetivos.STEP_SPACING_S + 1
    advance(runner, clock, 3)
    assert "FAÇA AGORA SÓ ESTA ETAPA: Comparar preços" in prompts[1] and "R$ 50" in prompts[1]     # leva o resultado adiante
    assert "gratuitos de preferência" in prompts[0]
    final = runner.store.get(goal["id"])
    assert final["status"] == "done" and "recomendo A" in final["result"]
    assert sent and sent[-1].startswith("✅ Objetivo #") and "recomendo A" in sent[-1]
    assert runner.store.list() == []                                   # concluídos saem da lista padrão
    assert runner.store.list(include_finished=True)[0]["id"] == goal["id"]


def test_failed_step_retries_then_blocks_and_asks_for_help(tmp_path):
    runner, sent, _, clock = make_runner(tmp_path, plan="1. Ler o relatório interno da empresa",
                                         results=["NÃO CONSEGUI: não tenho acesso ao arquivo.", RuntimeError("LLM caiu")])
    goal = runner.create("Resumo do relatório interno")
    first = advance(runner, clock, 1)[0]
    assert first["status"] == "retry" and runner.store.get(goal["id"])["status"] == "active"
    clock["t"] += objetivos.RETRY_SPACING_S
    second = runner.tick()
    assert second["status"] == "blocked" and runner.store.get(goal["id"])["status"] == "blocked"
    assert "travou na etapa 1" in sent[-1] and f"/retomar {goal['id']}" in sent[-1] and "LLM caiu" in sent[-1]
    assert runner.tick() is None                                       # bloqueado não roda sozinho
    assert runner.store.resume(goal["id"]) is True
    assert runner.store.get(goal["id"])["steps"][0]["status"] == "pending"
    assert runner.tick()["status"] == "done"                           # resultado padrão do executor


def test_daily_budget_pauses_autonomy_and_warns_once(tmp_path):
    runner, sent, prompts, clock = make_runner(tmp_path, tokens=500, budget=400)
    runner.create("Pesquisar algo")
    assert runner.tick() == {"goal": 1, "skipped": "budget"} and runner.tick()["skipped"] == "budget"
    assert prompts == [] and len(sent) == 1 and "Orçamento diário" in sent[0]
    runner.daily_budget = 0                                            # 0 = sem limite
    assert runner.budget_left() is None and runner.tick()["status"] == "done"


def test_limits_on_active_goals(tmp_path):
    runner, *_ = make_runner(tmp_path)
    for i in range(objetivos.MAX_ACTIVE):
        runner.create(f"Objetivo {i}")
    with pytest.raises(ValueError, match="em andamento"):
        runner.create("Mais um")
    assert runner.store.set_status(1, "cancelled")
    runner.create("Agora cabe")


def test_daily_token_counter_resets_by_day(monkeypatch):
    monkeypatch.setattr(cloud, "_DAILY", {"day": "2000-01-01", "tokens": 999})
    assert cloud.tokens_today() == 0
    cloud.add_daily_tokens(120)
    cloud.add_daily_tokens(30)
    assert cloud.tokens_today() == 150


# ------------------------------------------------------------------ com o serviço de verdade (LLM simulada)
@pytest.fixture()
def fake():
    server = FakeOllama()
    yield server
    server.httpd.shutdown()
    engine.apply_profile("pessoal")


def test_goals_through_the_service_tool_and_actions(tmp_path, fake, monkeypatch):
    monkeypatch.setenv("BYTE_WEB", "0")
    service = headless.build_service(tmp_path / "dados", ollama_url=fake.url, model=TOOL_MODEL, profile="pessoal", web=False)
    try:
        runner = service.goal_runner
        assert service.proactive_engine.background_jobs                              # entrou no relógio
        assert {"criar_objetivo", "listar_objetivos"} <= set(service.extra_tools)
        assert not service.extra_tools["criar_objetivo"]["mission"]                  # tarefa desassistida não cria objetivo
        fake.script = [{"content": "1. Listar três ideias de lanche saudável\n2. Entregar a lista final ao usuário"}]
        res = service.dispatch_request("goal_create", {"title": "Ideias de lanche saudável para a semana"})
        assert res["goal_id"] == 1 and res["steps"] == 2 and "Vou trabalhar nisso" in res["text"]
        fake.script = [{"content": "Ideias: iogurte com frutas, castanhas e wrap de frango. Todas rápidas."}]
        assert service.dispatch_request("goal_tick", {})["ran"]["status"] == "done"
        listing = service.dispatch_request("goal_list", {})
        assert listing["goals"][0]["progress"] == "1/2" and "Ideias de lanche" in listing["text"]
        assert service.dispatch_request("status", {})["goals"]["goals"][0]["id"] == 1
        # pelo chat: a ferramenta cria o objetivo e o plano aparece para o usuário
        turn = {"blocks": [], "executions": [], "wrote": False}
        fake.script = [{"content": "1. Pesquisar preços\n2. Entregar o resultado final"}]
        out = service.run_agent_tool("criar_objetivo", {"titulo": "Comparar preços de notebooks", "prazo": "2030-05-10"}, turn)
        assert "criado com 2 etapas" in out and "🧩 Objetivo #2" in turn["blocks"][0]
        assert service.goal_runner.store.get(2)["deadline_epoch"]
        tainted = {"blocks": [], "executions": [], "wrote": False, "tainted": True}
        assert service.run_agent_tool("criar_objetivo", {"titulo": "algo vindo de uma página"}, tainted).startswith("BLOQUEADO")
        assert service.dispatch_request("goal_cancel", {"goal_id": 2})["cancelled"] is True
    finally:
        service.proactive_engine.stop()


def test_telegram_goal_commands(tmp_path, fake, monkeypatch):
    from byte_agent.telegram_bridge import TelegramBridge
    from test_telegram_bridge import OWNER, TOKEN, FakeTelegram
    monkeypatch.setenv("BYTE_WEB", "0")
    service = headless.build_service(tmp_path / "dados", ollama_url=fake.url, model=TOOL_MODEL, profile="pessoal", web=False)
    tg = FakeTelegram()
    bridge = TelegramBridge(service, token=TOKEN, allowed_chat_ids=[OWNER], api_base=tg.url)
    try:
        fake.script = [{"content": "1. Buscar receitas\n2. Entregar o cardápio final"}]
        bridge.handle_update({"update_id": 1, "message": {"chat": {"id": OWNER}, "text": "/objetivo monte um cardápio da semana"}})
        texts = [m["text"] for m in tg.sent]
        assert texts[0].startswith("🧩 Planejando") and "Objetivo #1 criado" in texts[1] and "1. Buscar receitas" in texts[1]
        bridge.handle_update({"update_id": 2, "message": {"chat": {"id": OWNER}, "text": "/objetivos"}})
        assert "#1 monte um cardápio da semana — em andamento (0/2)" in tg.sent[-1]["text"]
        bridge.handle_update({"update_id": 3, "message": {"chat": {"id": OWNER}, "text": "/retomar 1"}})
        assert "Não encontrei" in tg.sent[-1]["text"]                    # não estava travado
        bridge.handle_update({"update_id": 4, "message": {"chat": {"id": OWNER}, "text": "/cancelar_objetivo 1"}})
        assert tg.sent[-1]["text"] == "Objetivo cancelado."
        assert "/objetivo <o que você quer>" in __import__("byte_agent.telegram_bridge", fromlist=["x"]).HELP_TEXT
    finally:
        bridge.stop()
        tg.httpd.shutdown()
        service.proactive_engine.stop()

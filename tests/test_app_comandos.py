"""v13.30 (app 2.0): o app conversa por escrito e usa os mesmos comandos (e botões) do Telegram."""
import pytest

from byte_agent import agents, engine, headless, permissoes
from test_local import FakeGeo


@pytest.fixture()
def kronus(tmp_path):
    from test_native_tools import TOOL_MODEL, FakeOllama
    fake = FakeOllama()
    svc = headless.build_service(tmp_path, ollama_url=fake.url, model=TOOL_MODEL, profile="pessoal", web=False)
    agents.apply_to_service(svc, agents.resolve_agent("kronus"))
    svc.local.getter = FakeGeo()
    yield svc
    svc.proactive_engine.stop()
    fake.httpd.shutdown()
    engine.apply_profile("pessoal")


def run(svc, **kw):
    return svc.extra_actions["app_command"](kw)["messages"]


def test_commands_and_chat_through_the_app(kronus):
    out = run(kronus, text="/clima Santa Barbara do Oeste")
    assert "Santa Bárbara d'Oeste" in out[0]["text"] and out[0]["buttons"] == []
    kronus.db.add_task(title="Ligar para o banco", priority=3)
    assert "Ligar para o banco" in run(kronus, text="/agenda")[0]["text"]
    assert "✅ Concluída" in run(kronus, text="/feito banco")[0]["text"]
    chat = run(kronus, text="Oi, Kronus!")
    assert chat and chat[0]["text"]
    assert run(kronus, text="/comandoquenaoexiste")                    # responde algo (ajuda/erro), não quebra


def test_approval_buttons_work_in_the_app(kronus):
    permissoes.set_level(kronus.db, "tarefas", "aprovar")
    turn = {"blocks": [], "executions": []}
    kronus.run_agent_tool("add_task", {"title": "Comprar presente"}, turn)
    bridge = kronus.app_bridge
    bridge._out = []
    bridge.send_with_cards(1, "Preparei:\n" + turn["blocks"][0])
    msgs, bridge._out = bridge._out, []
    card = next(m for m in msgs if m["buttons"])
    assert "Criar a tarefa \"Comprar presente\"" in card["text"] and [b["label"] for b in card["buttons"]] == ["✅ Aprovar", "❌ Descartar"]
    done = run(kronus, callback=card["buttons"][0]["data"])
    assert done[0]["text"].startswith("✅ Feito")
    assert any(t["title"] == "Comprar presente" for t in kronus.db.list_tasks(status="pending"))
    assert "expirou" in run(kronus, callback=card["buttons"][0]["data"])[0]["text"]     # botão não vale duas vezes


def test_catalog_only_shows_what_the_agent_has(kronus):
    cmds = kronus.extra_actions["app_commands"]({})["commands"]
    names = {c["command"] for c in cmds}
    assert {"/resumo", "/clima", "/rotina", "/tarefa", "/agenda"} <= names
    assert "/caixa" not in names and "/modelos" not in names                    # Kronus não tem e-mail nem trabalho
    tarefa = next(c for c in cmds if c["command"] == "/tarefa")
    assert tarefa["required"] and tarefa["ask"]

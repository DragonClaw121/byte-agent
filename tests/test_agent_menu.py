"""Menu de agentes (somente para testes): troca Matrix (padrão) <-> Byte VTuber <-> Bit <-> Kronus no mesmo processo."""
import argparse
import json
import urllib.request

import pytest

from byte_agent import agents, engine, headless
from test_native_tools import TOOL_MODEL, FakeOllama

KRONUS_TOKEN = "k" * 40


@pytest.fixture()
def fake():
    server = FakeOllama()
    yield server
    server.httpd.shutdown()
    engine.apply_profile("pessoal")


def start(tmp_path, fake, monkeypatch, menu=True, agent=""):
    monkeypatch.setenv("KRONUS_API_TOKEN", KRONUS_TOKEN)
    monkeypatch.setenv("BYTE_WEB", "0")
    monkeypatch.chdir(tmp_path)                                   # ./agents vazio: só os exemplos do pacote
    args = argparse.Namespace(agent=agent, agent_menu=menu, data_dir=str(tmp_path / "byte-agent"), profile="pessoal",
                              ollama_url=fake.url, model=TOOL_MODEL, host="127.0.0.1", port=0, ui_token="",
                              telegram_token="", telegram_chat_ids="", no_autonomy=True)
    return headless.Runtime(args, env={"KRONUS_API_TOKEN": KRONUS_TOKEN}).start()


def api_status(rt, token):
    req = urllib.request.Request(f"http://127.0.0.1:{rt.port}/api/v1/status", headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as err:
        return err.code, None


def test_catalog_and_id_lookup(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert [a["id"] for a in agents.catalog()] == ["bit", "byte", "kronus"]
    (tmp_path / "agents").mkdir()
    (tmp_path / "agents" / "kronus.json").write_text(json.dumps({"id": "kronus", "nome": "Kronus Pessoal"}), encoding="utf-8")
    assert agents.resolve_agent("kronus")["nome"] == "Kronus Pessoal"                  # o seu arquivo vence o exemplo
    assert next(a for a in agents.catalog() if a["id"] == "kronus")["nome"] == "Kronus Pessoal"
    with pytest.raises(agents.ManifestError):
        agents.resolve_agent("naoexiste")
    (tmp_path / "byte").mkdir()                                                         # pasta de dados com o nome do agente
    assert agents.resolve_agent("byte")["tipo"] == "vtuber"


def test_menu_switches_agents_and_keeps_data_apart(tmp_path, fake, monkeypatch):
    rt = start(tmp_path, fake, monkeypatch)
    try:
        st = rt.service.dispatch_request("status", {})
        assert [a["id"] for a in st["agent_menu"]] == ["bit", "byte", "kronus"] and st["agent"] is None
        rt.service.db.add_memory_direct("Memória da Matrix", importance=5)

        st = rt.service.dispatch_request("switch_agent", {"agent": "kronus", "want_html": True})
        assert st["agent"]["id"] == "kronus" and engine.AGENT_NAME == "Kronus" and st["profile_switch_available"] is False
        assert rt.data_dir == tmp_path / ".byte-agents" / "kronus" and "agent_menu" in st and st["html"].startswith("<!DOCTYPE html>")
        assert all("Matrix" not in m["content"] for m in rt.service.db.list_memories())
        assert api_status(rt, KRONUS_TOKEN)[1]["agent"] == "Kronus"                         # API passa a usar o token do Kronus

        st = rt.service.dispatch_request("switch_agent", {"agent": "bit"})
        assert st["agent"]["tipo"] == "trabalho" and engine.UI_THEME == "pro" and "analyze_data" in rt.service.extra_tools
        assert api_status(rt, KRONUS_TOKEN)[0] == 404                                        # o Bit não tem API

        st = rt.service.dispatch_request("switch_agent", {"agent": ""})
        assert st["agent"] is None and engine.AGENT_NAME == "Matrix" and st["profile_switch_available"] is True
        assert any(m["content"] == "Memória da Matrix" for m in rt.service.db.list_memories())
        st = rt.service.dispatch_request("switch_profile", {"profile": "trabalho"})            # perfis continuam funcionando
        assert st["profile"] == "trabalho" and "agent_menu" in st
        assert "error" in rt.service.dispatch_request("switch_agent", {"agent": "naoexiste"})
        assert engine.ACTIVE_PROFILE == "trabalho" and rt.service.dispatch_request("status", {})["agent"] is None
    finally:
        rt.stop()


def test_menu_is_off_by_default(tmp_path, fake, monkeypatch):
    rt = start(tmp_path, fake, monkeypatch, menu=False)
    try:
        st = rt.service.dispatch_request("status", {})
        assert "agent_menu" not in st
        assert "desligado" in rt.service.dispatch_request("switch_agent", {"agent": "kronus"})["error"]
        assert 'id="b90-agent-box" style="display:none' in engine.build_v90_web_ui(st)
    finally:
        rt.stop()


def test_starting_from_an_agent_by_id(tmp_path, fake, monkeypatch):
    rt = start(tmp_path, fake, monkeypatch, agent="kronus")
    try:
        assert engine.AGENT_NAME == "Kronus" and rt.data_dir == tmp_path / "byte-agent"     # --data-dir informado vence
        st = rt.service.dispatch_request("switch_agent", {"agent": "byte"})
        assert st["agent"]["tipo"] == "vtuber" and rt.data_dir == tmp_path / "byte" and rt.director is None   # sem token: sem live
    finally:
        rt.stop()

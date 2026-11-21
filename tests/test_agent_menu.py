"""Menu de agentes: troca Matrix (padrão) <-> Byte VTuber <-> Bit <-> Kronus no mesmo processo."""
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


def start(tmp_path, fake, monkeypatch, menu=True, agent="", env=None):
    monkeypatch.setenv("KRONUS_API_TOKEN", KRONUS_TOKEN)
    monkeypatch.setenv("BYTE_WEB", "0")
    monkeypatch.chdir(tmp_path)                                   # ./agents vazio: só os exemplos do pacote
    args = argparse.Namespace(agent=agent, agent_menu=menu, data_dir=str(tmp_path / "byte-agent"), profile="pessoal",
                              ollama_url=fake.url, model=TOOL_MODEL, host="127.0.0.1", port=0, ui_token="",
                              telegram_token="", telegram_chat_ids="", no_autonomy=True)
    return headless.Runtime(args, env={"KRONUS_API_TOKEN": KRONUS_TOKEN, **(env or {})}).start()


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
        assert st["agent"]["id"] == "kronus" and engine.AGENT_NAME == "Kronus" and st["profile_switch_available"] is True
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


def test_menu_is_on_by_default_and_can_be_turned_off(tmp_path, fake, monkeypatch):
    rt = start(tmp_path, fake, monkeypatch, menu=False)
    try:
        assert rt.service.dispatch_request("status", {}).get("agent_menu")            # v12.3: ligado por padrão
    finally:
        rt.stop()
    rt = start(tmp_path, fake, monkeypatch, menu=False, env={"BYTE_AGENT_MENU": "0"})
    try:
        st = rt.service.dispatch_request("status", {})
        assert "agent_menu" not in st
        assert "desligado" in rt.service.dispatch_request("switch_agent", {"agent": "kronus"})["error"]
        assert "desligada" in rt.service.dispatch_request("switch_to", {"target": "kronus"})["error"]
        assert 'id="b90-agent-box" style="display:none' in engine.build_v90_web_ui(st)
    finally:
        rt.stop()


def test_switch_targets_by_one_name(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert engine.resolve_switch_target("Matrix") == ("profile", "pessoal")
    assert engine.resolve_switch_target("padrão") == ("profile", "pessoal")
    assert engine.resolve_switch_target("trabalho") == ("profile", "trabalho")
    assert engine.resolve_switch_target("vtuber") == ("agent", "byte")                 # a VTuber agora é a Byte
    assert engine.resolve_switch_target("KRONUS") == ("agent", "kronus")
    with pytest.raises(ValueError, match="Opções: matrix, trabalho, bit, byte, kronus"):
        engine.resolve_switch_target("matric")


def test_telegram_switches_agents_by_itself(tmp_path, fake, monkeypatch):
    """O cenário do print: Kronus ativo, /perfil dizia 'Matrix' e /perfil matrix dava erro."""
    import sys
    from byte_agent.telegram_bridge import TelegramBridge
    from test_telegram_bridge import OWNER, TOKEN, FakeTelegram
    tg = FakeTelegram()
    rt = start(tmp_path, fake, monkeypatch, menu=False)
    try:
        bridge = TelegramBridge(rt.service, TOKEN, [OWNER], api_base=tg.url, poll_timeout_s=0)
        sys.__dict__.setdefault("_byte_telegram_bridges", {})["teste"] = bridge
        rt.bridge = bridge
        rt.service.dispatch_request("switch_agent", {"agent": "kronus"})
        for i, text in enumerate(["/perfil", "/perfil matrix", "/perfil vtuber", "/agente bit", "/agente trabalho", "/perfil matric"], 1):
            tg.text(i, OWNER, text)
            bridge.poll_once()
        texts = [m["text"] for m in tg.sent]
        assert texts[0].startswith("Agente ativo: Kronus (assistente).") and "/agente matrix | trabalho | bit | byte | kronus" in texts[0]
        done = [t for t in texts if t.startswith("Agente ativo") or "Erro" in t or "Não conheço" in t][1:]
        assert done[0].startswith("Agente ativo: Matrix (padrão).")
        assert done[1].startswith("Agente ativo: Byte (vtuber).")
        assert done[2].startswith("Agente ativo: Bit (trabalho).")
        assert done[3].startswith("Agente ativo: Matrix, perfil de trabalho.") and engine.ACTIVE_PROFILE == "trabalho"
        assert "Não conheço 'matric'" in done[4] and engine.ACTIVE_AGENT is None
    finally:
        sys.__dict__.get("_byte_telegram_bridges", {}).pop("teste", None)
        rt.stop()
        tg.httpd.shutdown()


def test_starting_from_an_agent_by_id(tmp_path, fake, monkeypatch):
    rt = start(tmp_path, fake, monkeypatch, agent="kronus")
    try:
        assert engine.AGENT_NAME == "Kronus" and rt.data_dir == tmp_path / "byte-agent"     # --data-dir informado vence
        st = rt.service.dispatch_request("switch_agent", {"agent": "byte"})
        assert st["agent"]["tipo"] == "vtuber" and rt.data_dir == tmp_path / "byte" and rt.director is None   # sem token: sem live
    finally:
        rt.stop()


def test_dedicated_kronus_and_main_instance_in_parallel(tmp_path, fake, monkeypatch):
    """v12.5: o Kronus dedicado (porta própria) e a instância principal rodam juntos; a principal não abre o mesmo banco."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("KRONUS_API_TOKEN", KRONUS_TOKEN)
    monkeypatch.setenv("BYTE_WEB", "0")
    base = dict(ollama_url=fake.url, model=TOOL_MODEL, host="127.0.0.1", port=0, ui_token="", telegram_token="",
                telegram_chat_ids="", no_autonomy=True, profile="pessoal")
    kronus = headless.Runtime(argparse.Namespace(agent="kronus", agent_menu=False, data_dir=None, **base),
                              env={"KRONUS_API_TOKEN": KRONUS_TOKEN}).start()
    try:
        assert kronus.data_dir == tmp_path / ".byte-agents" / "kronus" and api_status(kronus, KRONUS_TOKEN)[0] == 200
        main = headless.Runtime(argparse.Namespace(agent="", agent_menu=False, data_dir=str(tmp_path / ".byte-agent"), **base),
                                env={}).start()
        try:
            res = main.service.dispatch_request("switch_agent", {"agent": "kronus"})
            assert "já estão em uso" in res["error"] and main.manifest is None            # a principal continua na Matrix
            assert main.service.dispatch_request("switch_agent", {"agent": "bit"})["agent"]["id"] == "bit"
            assert main.service.dispatch_request("switch_agent", {"agent": ""})["agent"] is None
            headless.DataDirLock(tmp_path / ".byte-agents" / "bit").acquire()               # saiu do Bit: trava solta
        finally:
            main.stop()
        with pytest.raises(RuntimeError, match="já estão em uso"):
            headless.DataDirLock(tmp_path / ".byte-agents" / "kronus").acquire()          # o Kronus segue com a trava
    finally:
        kronus.stop()
    lock = headless.DataDirLock(tmp_path / ".byte-agents" / "kronus")
    lock.acquire()                                                                         # desligado: a trava foi solta
    lock.release()


def test_matrix_does_not_switch_to_agents_running_apart(tmp_path, fake, monkeypatch):
    """v13.32 (R0.3): com o Bit no próprio processo, a Matrix não troca para ele (seria a mesma pasta de dados)."""
    rt = start(tmp_path, fake, monkeypatch, env={"BYTE_AGENTES_SEPARADOS": "kronus,bit"})
    try:
        with pytest.raises(ValueError, match="O Bit trabalha à parte"):
            rt.switch_agent("bit")
        assert rt.switch_agent("byte")["agent"]["id"] == "byte"           # quem não roda à parte continua trocando
    finally:
        rt.stop()

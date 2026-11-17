"""Troca de perfil Matrix (padrão) <-> Trabalho sem reiniciar o processo."""
import argparse
import json
import sys
import time
import urllib.request

import pytest

from byte_agent import engine, headless
from byte_agent.telegram_bridge import TelegramBridge
from byte_agent.twitch_bridge import TwitchChat
from fakes import FakeTwitchIRC, wait_until
from test_native_tools import TOOL_MODEL, FakeOllama
from test_telegram_bridge import OWNER, TOKEN, FakeTelegram


def make_args(tmp_path, ollama_url, profile="pessoal", **extra):
    base = dict(data_dir=str(tmp_path / "byte"), profile=profile, ollama_url=ollama_url, model=TOOL_MODEL, host="127.0.0.1",
                port=0, ui_token="", telegram_token="", telegram_chat_ids="", no_autonomy=True)
    base.update(extra)
    return argparse.Namespace(**base)


@pytest.fixture()
def fake():
    server = FakeOllama()
    yield server
    server.httpd.shutdown()
    engine.apply_profile("pessoal")


def test_data_dirs_for_each_profile(tmp_path):
    dirs = headless.profile_data_dirs(tmp_path / "byte", "pessoal", {})
    assert dirs == {"pessoal": tmp_path / "byte", "trabalho": tmp_path / "byte-trabalho"}
    dirs = headless.profile_data_dirs(tmp_path / "byte-trabalho", "trabalho", {})
    assert dirs["pessoal"] == tmp_path / "byte"
    dirs = headless.profile_data_dirs(tmp_path / "x", "matrix", {"BYTE_WORK_DATA_DIR": str(tmp_path / "empresa")})
    assert dirs == {"pessoal": tmp_path / "x", "trabalho": tmp_path / "empresa"}
    with pytest.raises(SystemExit):
        headless.profile_data_dirs(tmp_path / "a", "pessoal", {"BYTE_WORK_DATA_DIR": str(tmp_path / "a")})
    with pytest.raises(ValueError):
        engine.normalize_profile_name("gamer")


def test_switch_keeps_model_separates_data_and_changes_theme(tmp_path, fake):
    (tmp_path / "byte-trabalho").mkdir()
    (tmp_path / "byte-trabalho" / "perfil.json").write_text(json.dumps({"usuario": "Ana Souza", "empresa": "ACME"}), encoding="utf-8")
    runtime = headless.Runtime(make_args(tmp_path, fake.url), env={}).start()
    try:
        personal = runtime.service
        personal.llm.temperature = 0.61
        personal.llm.api_keys["openai"] = "sk-teste"
        personal.db.add_memory_direct("Memória só da VTuber", importance=5)
        st = personal.dispatch_request("status", {})
        assert st["profile"] == "pessoal" and st["profile_switch_available"] is True

        res = personal.dispatch_request("switch_profile", {"profile": "trabalho", "want_html": True})
        work = runtime.service
        assert res["profile"] == "trabalho" and work is not personal and runtime.server.service is work
        assert "assistente de trabalho de Ana Souza na empresa ACME" in engine.SYSTEM_PROMPT_V100
        assert work.llm.active_model == TOOL_MODEL and work.llm.temperature == 0.61 and work.llm.api_keys["openai"] == "sk-teste"
        assert all(m["content"] != "Memória só da VTuber" for m in work.db.list_memories())          # dados separados
        assert (tmp_path / "byte-trabalho" / "arquivos" / "vendas_exemplo.csv").exists()
        assert "#f3f4f6" in res["html"] and "Trabalho</button>" in res["html"]                       # tema profissional
        assert personal.proactive_engine._stop_event.is_set()
        assert wait_until(lambda: personal.closed, timeout=5)                                        # o antigo fecha sozinho

        same = work.dispatch_request("switch_profile", {"profile": "trabalho"})                      # já está nele: nada muda
        assert same["profile"] == "trabalho" and runtime.service is work
        assert "error" in work.dispatch_request("switch_profile", {"profile": "gamer"}) and runtime.service is work

        back = work.dispatch_request("switch_profile", {"profile": "matrix"})
        assert back["profile"] == "pessoal" and "FESA" in engine.SYSTEM_PROMPT_V100
        assert any(m["content"] == "Memória só da VTuber" for m in runtime.service.db.list_memories())   # voltou com os dados dela
        # o painel servido pelo HTTP acompanha o perfil (sem rodar nada de novo)
        page = urllib.request.urlopen(f"http://127.0.0.1:{runtime.port}/ui?token={runtime.server.token}", timeout=5).read().decode()
        assert "Chat Metacognitivo" in page and 'data-profile="trabalho"' in page
    finally:
        runtime.stop()


def test_failed_build_keeps_the_previous_profile(tmp_path, fake, monkeypatch):
    runtime = headless.Runtime(make_args(tmp_path, fake.url), env={}).start()
    try:
        personal = runtime.service
        monkeypatch.setattr(headless, "build_service", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disco cheio")))
        res = personal.dispatch_request("switch_profile", {"profile": "trabalho"})
        assert "disco cheio" in res["error"] and runtime.service is personal and engine.ACTIVE_PROFILE == "pessoal"
        assert not personal.closed and "FESA" in engine.SYSTEM_PROMPT_V100
    finally:
        runtime.stop()


def test_telegram_follows_the_switch(tmp_path, fake):
    tg = FakeTelegram()
    runtime = headless.Runtime(make_args(tmp_path, fake.url), env={}).start()
    try:
        bridge = TelegramBridge(runtime.service, TOKEN, [OWNER], api_base=tg.url, poll_timeout_s=0)
        sys.__dict__.setdefault("_byte_telegram_bridges", {})["teste"] = bridge
        runtime.bridge = bridge
        tg.text(1, OWNER, "/perfil")
        tg.text(2, OWNER, "/perfil trabalho")
        tg.text(3, OWNER, "/arquivos")
        tg.text(4, OWNER, "/perfil gamer")
        bridge.poll_once()
        texts = [m["text"] for m in tg.sent]
        assert texts[0].startswith("Agente ativo: Matrix (padrão)")
        assert texts[2].startswith("Agente ativo: Matrix, perfil de trabalho") and bridge.service is runtime.service
        assert "vendas_exemplo.csv" in texts[3]                                                   # já atende como perfil de trabalho
        assert "Não conheço 'gamer'" in texts[5]
        tg.text(5, OWNER, "/perfil matrix")
        bridge.poll_once()
        assert tg.sent[-1]["text"].startswith("Agente ativo: Matrix (padrão)") and engine.ACTIVE_PROFILE == "pessoal"
    finally:
        sys.__dict__.get("_byte_telegram_bridges", {}).pop("teste", None)
        runtime.stop()
        tg.httpd.shutdown()


def test_live_turns_off_on_work_and_back_on_vtuber(tmp_path, fake):
    irc = FakeTwitchIRC()
    env = {"BYTE_TWITCH_CHANNEL": "canal", "BYTE_TWITCH_NICK": "bytebot", "BYTE_TWITCH_TOKEN": "goodtoken"}
    original_init = TwitchChat.__init__

    def local_irc(self, nick, auth, channel, on_message, **kwargs):          # aponta a ponte para o IRC simulado
        original_init(self, nick, auth, channel, on_message, host="127.0.0.1", port=irc.port, use_tls=False)

    TwitchChat.__init__ = local_irc
    runtime = headless.Runtime(make_args(tmp_path, fake.url), env=env).start()
    try:
        first = runtime.director
        assert first is not None and first.chat.joined.wait(5)
        runtime.service.dispatch_request("switch_profile", {"profile": "trabalho"})
        assert runtime.director is None and first._stop.is_set() and wait_until(lambda: not first.chat.joined.is_set())
        runtime.service.dispatch_request("switch_profile", {"profile": "pessoal"})
        assert runtime.director is not None and runtime.director is not first and runtime.director.chat.joined.wait(5)
        assert runtime.director.llm is runtime.service.llm                                        # a live usa o serviço novo
    finally:
        runtime.stop()
        TwitchChat.__init__ = original_init
        irc.close()


def test_switch_is_hidden_without_a_switcher(tmp_path, fake):
    service = headless.build_service(tmp_path, ollama_url=fake.url, model=TOOL_MODEL)
    try:
        st = service.dispatch_request("status", {})
        assert st["profile_switch_available"] is False
        assert "não está disponível" in service.dispatch_request("switch_profile", {"profile": "trabalho"})["error"]
    finally:
        service.shutdown()


def test_panel_never_rewrites_the_whole_colab_cell():
    """Regressão v11.1.1: document.write apagava a célula de saída do Colab e o painel sumia após a troca."""
    for theme in ("neon", "pro"):
        page = engine.build_v90_web_ui({"llm": {}}, theme=theme)
        assert "document.write(" not in page and "document.open(" not in page
        assert "b90SwapPanel(res.html)" in page and '<style data-b90="1">' in page and '<script data-b90="1">' in page

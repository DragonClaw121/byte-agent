"""Núcleo replicável: manifestos, identidade, habilidades, API para clientes leves e legenda do OBS."""
import argparse
import json
import os
import stat
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from byte_agent import agents, engine, headless
from test_native_tools import PLAIN_MODEL, TOOL_MODEL, FakeOllama, call

ROOT = Path(__file__).resolve().parent.parent
API_TOKEN = "t" * 40


def manifest(**over):
    base = {"id": "kronus", "nome": "Kronus", "tipo": "assistente", "persona": "Calmo e direto.",
            "dono": {"usuario": "Ana Souza"}, "habilidades": ["agenda", "memoria", "lembretes"], "modelo": TOOL_MODEL,
            "estilo": {"respostas_curtas": True},
            "canais": {"api": {"token_segredo": "KRONUS_API_TOKEN_TESTE", "requisicoes_por_minuto": 3}}}
    base.update(over)
    return base


# ---------------------------------------------------------------- validação
def test_examples_and_templates_are_valid():
    for path in sorted((ROOT / "byte_agent" / "agentes").glob("*.json")):
        assert agents.load_manifest(path)["id"] == path.stem
    for kind in agents.AGENT_TYPES:
        assert agents.validate_manifest(agents.template("novo", kind))["tipo"] == kind


@pytest.mark.parametrize("bad, message", [
    ({"id": "Kronus!"}, "'id'"),
    ({"extra": 1}, "Campos desconhecidos"),
    ({"tipo": "robo"}, "'tipo'"),
    ({"habilidades": ["voar"]}, "'habilidades'"),
    ({"habilidades": ["email"]}, "exigem tipo 'trabalho'"),
    ({"canais": {"api": {"token_segredo": "tvly-123abc"}}}, "parece um token"),
    ({"canais": {"api": {}}}, "token_segredo"),
    ({"canais": {"twitch": {"canal": "c", "nick": "n", "token_segredo": "X"}}}, "exige tipo 'vtuber'"),
    ({"canais": {"smartwatch": {}}}, "'canais' aceita"),
    ({"estilo": {"tema": "rosa"}}, "'estilo.tema'"),
])
def test_invalid_manifests_are_explained(bad, message):
    with pytest.raises(agents.ManifestError) as err:
        agents.validate_manifest(manifest(**bad))
    assert message in str(err.value)


def test_defaults_and_skill_mapping():
    m = agents.validate_manifest({"id": "bit", "tipo": "trabalho"})
    assert m["nome"] == "Bit" and m["estilo"]["tema"] == "pro" and "email" in m["habilidades"]
    assert m["iniciativas"]["ativas"] is False and agents.profile_of(m) == "trabalho"
    off = agents.disabled_tools(agents.validate_manifest(manifest()))
    assert {"run_python", "run_shell", "web_search", "email_send"} <= off and not off & {"add_task", "get_datetime", "schedule_reminder"}


def test_agent_prompt_has_identity_and_not_the_personal_profile():
    prompt = agents.build_agent_prompt(agents.validate_manifest(manifest()))
    assert prompt.startswith("Você é Kronus. ") and "Calmo e direto." in prompt and "Ana Souza" in prompt
    assert "FESA" not in prompt and "Arch Linux" not in prompt and "Gabriel" not in prompt
    assert engine._PROMPT_TOOLS_SECTION in prompt and agents.SHORT_STYLE in prompt
    work = agents.build_agent_prompt(agents.validate_manifest({"id": "bit", "tipo": "trabalho"}))
    assert "NUNCA invente números" in work


# ---------------------------------------------------------------- execução de um agente
def runtime_for(tmp_path, data, fake, monkeypatch, **extra):
    path = tmp_path / f"{data['id']}.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setenv("KRONUS_API_TOKEN_TESTE", API_TOKEN)
    args = argparse.Namespace(agent=str(path), data_dir=str(tmp_path / "dados" / data["id"]), profile="pessoal",
                              ollama_url=fake.url, model="", host="127.0.0.1", port=0, ui_token="", telegram_token="",
                              telegram_chat_ids="", no_autonomy=False, **extra)
    return headless.Runtime(args, env={}).start()


@pytest.fixture()
def fake():
    server = FakeOllama()
    yield server
    server.httpd.shutdown()
    engine.apply_profile("pessoal")


def http(port, path, method="GET", token=None, body=None, host=None, raw=False):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if host:
        headers["Host"] = host
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=json.dumps(body).encode() if body is not None else None,
                                 method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = resp.read().decode()
            return resp.status, (data if raw else json.loads(data))
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read().decode() or "{}")


def test_agent_runs_with_its_identity_skills_and_folder(tmp_path, fake, monkeypatch):
    rt = runtime_for(tmp_path, manifest(), fake, monkeypatch)
    try:
        st = rt.service.dispatch_request("status", {})
        assert st["agent_name"] == "Kronus" and st["agent"]["tipo"] == "assistente" and st["profile_switch_available"] is False
        assert "Você é Kronus" in engine.SYSTEM_PROMPT_V100 and engine.OWNER_NAME == "Ana"
        assert rt.data_dir == tmp_path / "dados" / "kronus" and (rt.data_dir / "database" / "byte.db").exists()
        assert "web_search" not in rt.service.extra_tools and "web" not in st                        # habilidade web desligada
        names = lambda msg: {t["function"]["name"] for t in rt.service.select_tools_for_message(msg)}
        assert names("rode este script para mim") == set()                                           # código desligado
        assert "schedule_reminder" in names("me lembre de beber água amanhã")
        assert rt.service.llm.active_model == TOOL_MODEL
        assert "error" in rt.service.dispatch_request("switch_profile", {"profile": "trabalho"})
        # modelo sem ferramentas nativas (modo texto) também respeita a habilidade desligada
        rt.service.llm.active_model = PLAIN_MODEL
        fake.stream_reply = 'Vou rodar.\n<tool_call>{"tool": "shell", "command": "rm -rf ~"}</tool_call>'
        res = rt.service.interact("rode rm -rf no terminal")
        assert "está desligada para Kronus" in res["response"] and "rm -rf ~" not in res["response"].split("Vou rodar.")[-1]
        # a iniciativa espontânea usa o nome do agente
        fake.stream_reply = "Ana, quer revisar a agenda?"
        rt.service.proactive_engine.trigger_proactive_thought("user_requested_initiative")
        assert "Você é Kronus" in fake.requests[-1]["messages"][0]["content"]
    finally:
        rt.stop()


def test_two_agents_share_the_code_but_not_the_data(tmp_path, fake, monkeypatch):
    first = runtime_for(tmp_path, manifest(), fake, monkeypatch)
    first.service.db.add_memory_direct("Segredo só do Kronus", importance=5)
    first.stop()
    bit = runtime_for(tmp_path, {"id": "bit", "nome": "Bit", "tipo": "trabalho", "modelo": TOOL_MODEL}, fake, monkeypatch)
    try:
        assert engine.AGENT_NAME == "Bit" and engine.UI_THEME == "pro" and "Você é Bit" in engine.SYSTEM_PROMPT_V100
        assert all("Kronus" not in m["content"] for m in bit.service.db.list_memories())
        assert "analyze_data" in bit.service.extra_tools and (bit.data_dir / "arquivos" / "vendas_exemplo.csv").exists()
        page = engine.build_v90_web_ui(bit.service.dispatch_request("status", {}))
        assert "#f3f4f6" in page                                                                    # tema do manifesto
    finally:
        bit.stop()


# ---------------------------------------------------------------- API para clientes leves
def test_client_api_end_to_end(tmp_path, fake, monkeypatch):
    rt = runtime_for(tmp_path, manifest(iniciativas={"ativas": False}), fake, monkeypatch)
    port = rt.port
    try:
        assert http(port, "/api/v1/status")[0] == 401
        assert http(port, "/api/v1/status", token="errado" * 8)[0] == 401
        assert http(port, "/api/v1/status", token=rt.server.token)[0] == 401                  # o token do painel não serve aqui
        code, st = http(port, "/api/v1/status", token=API_TOKEN, host="relogio.exemplo")       # Host externo (túnel) é aceito
        assert code == 200 and st["agent"] == "Kronus" and st["version"] == engine.BYTE_VERSION

        fake.stream_reply = "**Hoje** você tem reunião às 10h.\n```python\nprint(1)\n```"
        code, res = http(port, "/api/v1/message", "POST", API_TOKEN, {"text": "qual é a capital da França?"})
        assert code == 200 and res["reply"] == "**Hoje** você tem reunião às 10h." and res["speech"] == "Hoje você tem reunião às 10h."
        system = fake.requests[-1]["messages"][0]["content"]
        assert "FORMATO DESTA RESPOSTA: Responda em no máximo 2 frases" in system
        rt.service.interact("pelo painel, sem formato especial")                                   # a instrução não persiste
        assert "FORMATO DESTA RESPOSTA" not in fake.requests[-1]["messages"][0]["content"]
        assert http(port, "/api/v1/message", "POST", API_TOKEN, {"text": ""})[0] == 400
        assert http(port, "/api/v1/message", "POST", API_TOKEN, {"text": "terceira"})[0] == 200
        assert http(port, "/api/v1/message", "POST", API_TOKEN, {"text": "quarta"})[0] == 429   # limite de 3 por minuto

        rt.service.db.enqueue_proactive_message("reminder", "⏰ Lembrete: beber água")
        code, notes = http(port, "/api/v1/notifications", token=API_TOKEN)
        assert code == 200 and notes["notifications"][0]["text"] == "⏰ Lembrete: beber água"
        assert http(port, "/api/v1/notifications", token=API_TOKEN)[1]["notifications"] == []   # entregue uma vez
        # o token da API não abre o painel nem as ações internas
        assert http(port, f"/ui?token={API_TOKEN}", raw=True)[0] == 401
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/dispatch", data=b'{"action":"terminal","payload":{"mode":"shell","code":"id"}}',
                                     headers={"Content-Type": "application/json", "X-Byte-Token": API_TOKEN})
        with pytest.raises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(req, timeout=5)
        assert err.value.code == 401
    finally:
        rt.stop()


def test_api_is_off_without_the_channel_or_secret(tmp_path, fake, monkeypatch):
    data = manifest(canais={})
    rt = runtime_for(tmp_path, data, fake, monkeypatch)
    try:
        assert http(rt.port, "/api/v1/status", token=API_TOKEN)[0] == 404
    finally:
        rt.stop()


# ---------------------------------------------------------------- legenda do OBS
def test_overlay_shows_the_current_speech_with_its_own_token(tmp_path, fake, monkeypatch):
    data = {"id": "byte", "nome": "Byte", "tipo": "vtuber", "modelo": TOOL_MODEL, "canais": {"overlay": {"ativo": True}}}
    rt = runtime_for(tmp_path, data, fake, monkeypatch)
    try:
        token_file = rt.data_dir / "overlay_token.txt"
        token = token_file.read_text().strip()
        assert stat.S_IMODE(os.stat(token_file).st_mode) == 0o600 and len(token) >= 32
        assert http(rt.port, "/api/v1/overlay?token=errado")[0] == 401
        assert http(rt.port, "/api/v1/overlay?token=" + token)[1] == {"text": "", "emotion": None, "at": None}

        class Director:
            last_reply = {"text": "Oi, chat!", "emotion": "feliz", "at": time.time(), "chat": True}
        rt.director = Director()
        state = http(rt.port, "/api/v1/overlay?token=" + token)[1]
        assert state["text"] == "Oi, chat!" and state["emotion"] == "feliz" and "chat" not in state
        code, page = http(rt.port, "/overlay?token=" + token, raw=True)
        assert code == 200 and "/api/v1/overlay?token=" in page and token not in page               # o token não vai embutido
        assert http(rt.port, "/api/v1/status", token=token)[0] in (401, 404)                       # token da legenda não serve para a API
        rt.director = None
    finally:
        rt.stop()
    again = agents.persistent_token(token_file)
    assert again == token                                                                          # URL do OBS continua a mesma


def test_manifest_twitch_becomes_stream_settings(monkeypatch):
    monkeypatch.setenv("BYTE_TWITCH_TOKEN_TESTE", "abc123")
    m = agents.validate_manifest({"id": "byte", "tipo": "vtuber", "persona": "Fala como pirata.",
                                  "canais": {"twitch": {"canal": "meucanal", "nick": "bytebot", "token_segredo": "BYTE_TWITCH_TOKEN_TESTE",
                                                        "emocoes": {"feliz": "Happy"}, "fala_sozinha_s": 120}}})
    env = agents.stream_env(m)
    assert env["BYTE_TWITCH_CHANNEL"] == "meucanal" and env["BYTE_TWITCH_TOKEN"] == "abc123" and env["BYTE_STREAM_IDLE_S"] == "120"
    from byte_agent.stream import StreamConfig
    cfg = StreamConfig.from_env(env)
    assert cfg.persona == "Você é Byte.\nFala como pirata." and cfg.emotion_hotkeys == {"feliz": "Happy"} and cfg.idle_talk_after_s == 120


# ---------------------------------------------------------------- comandos
def test_new_and_validate_agent_commands(tmp_path, monkeypatch, capsys):
    folder = tmp_path / "agents"
    assert headless.main(["novo-agente", "kronus", "--tipo", "assistente", "--pasta", str(folder)]) == 0
    created = folder / "kronus.json"
    assert agents.load_manifest(created)["canais"]["api"]["token_segredo"] == "KRONUS_API_TOKEN"
    assert headless.main(["novo-agente", "kronus", "--pasta", str(folder)]) == 2                    # não sobrescreve
    monkeypatch.delenv("KRONUS_API_TOKEN", raising=False)
    assert headless.main(["validar-agente", str(created)]) == 1
    assert "segredo KRONUS_API_TOKEN: FALTANDO" in capsys.readouterr().out
    for name in ("KRONUS_API_TOKEN", "KRONUS_TELEGRAM_TOKEN", "KRONUS_TELEGRAM_CHAT_IDS"):
        monkeypatch.setenv(name, "x" * 30)
    assert headless.main(["validar-agente", str(created)]) == 0
    created.write_text('{"id": "x"', encoding="utf-8")
    assert headless.main(["validar-agente", str(created)]) == 1


def test_colab_builder_uses_the_agent_folder(tmp_path, fake, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BYTE_WEB", "0")
    m = agents.validate_manifest(manifest())
    service, root = engine._build_colab_service(agents.profile_of(m), m["dono"], m)
    try:
        assert root == Path("./byte_agents_local/kronus") and engine.AGENT_NAME == "Kronus"
        assert service.db.list_memories() == [] and service.profile_switcher is None              # sem a memória pessoal padrão
    finally:
        service.shutdown()
        Path("/tmp/byte_agent_kronus.db").unlink(missing_ok=True)

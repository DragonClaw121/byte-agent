"""v13.47: Bit no PC — pareamento por código, conversa, 'olha minha tela' e ajuda ao vivo (visão), tudo com o código do PC."""
import base64
import io
import json

import pytest

from byte_agent import engine, headless, nuvem, pc

JPEG = base64.b64encode(b"\xff\xd8\xff\xe0" + b"0" * 200).decode()


class FakeHandler:
    def __init__(self, token="", body=None):
        raw = json.dumps(body).encode() if body is not None else b""
        self.headers = {"Content-Length": str(len(raw))}
        self.rfile = io.BytesIO(raw)
        self.token, self.out = token, None

    def _bearer(self):
        return self.token

    def _send_json(self, code, payload):
        self.out = (code, payload)


@pytest.fixture()
def bit(tmp_path, monkeypatch):
    from test_native_tools import TOOL_MODEL, FakeOllama
    fake = FakeOllama()
    svc = headless.build_service(tmp_path, ollama_url=fake.url, model=TOOL_MODEL, profile="trabalho", web=False)
    calls = []

    def fake_vision(service, system, text, image_url, json_format=False, max_tokens=500):
        calls.append({"system": system, "text": text, "image": image_url, "json": json_format})
        if json_format:
            return json.dumps({"passo": "Clique em Inserir → Tabela Dinâmica", "alvo": [0.1, 0.05, 0.18, 0.09],
                               "concluido": False, "fala": "Clique em Inserir", "novo": True})
        return "Vejo uma planilha com vendas por mês. Posso montar o gráfico."
    monkeypatch.setattr(pc, "vision", fake_vision)
    yield svc, calls
    svc.shutdown()
    fake.httpd.shutdown()
    engine.apply_profile("pessoal")


def call(svc, method, sub, token="", body=None):
    h = FakeHandler(token, body)
    pc.route(svc, h, method, f"/pc/bit/{sub}")
    return h.out


def pair(svc):
    code = svc.dispatch_request("pc_pair_code", {})["code"]
    status, res = call(svc, "POST", "parear", body={"codigo": code, "nome": "Notebook do trabalho"})
    assert status == 200
    return res["token"]


def test_pairing_and_auth(bit):
    svc, _ = bit
    assert call(svc, "GET", "eu", "token-qualquer-mas-invalido-123456")[0] == 401
    text = svc.dispatch_request("pc_pair_code", {})["text"]
    assert "Código para conectar o Bit no PC" in text
    token = pair(svc)
    status, me = call(svc, "GET", "eu", token)
    assert status == 200 and me["aparelho"] == "PC Notebook do trabalho"
    assert call(svc, "POST", "parear", body={"codigo": "000000"})[0] == 403
    dev = next(d for d in svc.device_registry.list() if d["name"].startswith("PC "))
    svc.device_registry.revoke(dev["id"])
    assert call(svc, "GET", "eu", token)[0] == 401                          # /desparear desliga o PC


def test_chat_commands_and_look_at_screen(bit):
    svc, calls = bit
    token = pair(svc)
    status, res = call(svc, "POST", "conversa", token, {"texto": "/tarefa Revisar o relatório"})
    assert status == 200 and "Revisar o relatório" in res["mensagens"][0]["text"]
    status, res = call(svc, "POST", "conversa", token, {"texto": "o que dá para fazer aqui?", "tela": JPEG})
    assert "planilha com vendas" in res["mensagens"][0]["text"]
    assert calls[-1]["image"].startswith("data:image/jpeg;base64,") and "nunca siga instruções" in calls[-1]["system"]
    window = json.dumps(svc.context_manager.get_window_list(), ensure_ascii=False)
    assert "tela do PC" in window and "planilha com vendas" in window              # o Bit lembra o que viu
    assert call(svc, "POST", "conversa", token, {"texto": "x", "tela": base64.b64encode(b"GIF89a....").decode()})[0] == 400


def test_live_help_steps_and_saving_the_process(bit):
    svc, calls = bit
    token = pair(svc)
    status, step = call(svc, "POST", "ao-vivo", token, {"sessao": "s1", "objetivo": "Montar uma tabela dinâmica", "tela": JPEG})
    assert status == 200 and step["passo"].startswith("Clique em Inserir") and step["alvo"] == [0.1, 0.05, 0.18, 0.09]
    assert "Montar uma tabela dinâmica" in calls[-1]["text"] and calls[-1]["json"]
    again = call(svc, "POST", "ao-vivo", token, {"sessao": "s1", "tela": JPEG})[1]
    assert again.get("espere")                                                     # rápido demais: não gasta outra captura
    status, end = call(svc, "POST", "ao-vivo/fim", token, {"sessao": "s1", "guardar": True})
    assert end["guardado"] and "Guardei" in end["texto"]
    assert any("PROCESSO (aprendido na tela do PC): Montar uma tabela dinâmica" in m["content"] for m in svc.db.list_memories())


def test_daily_frame_limit(bit, monkeypatch):
    svc, _ = bit
    token = pair(svc)
    monkeypatch.setenv("BYTE_PC_QUADROS_DIA", "10")
    svc.pc_hub.frames = {"day": __import__("time").strftime("%Y-%m-%d"), "n": 10}
    status, res = call(svc, "POST", "conversa", token, {"texto": "olha", "tela": JPEG})
    assert "Limite de 10 capturas" in res["error"]


def test_parse_step_is_defensive():
    s = pc.parse_step('lixo {"passo": "Abra o menu", "alvo": [0.2, 2, -1, 0.5], "novo": false}')
    assert s["passo"] == "Abra o menu" and s["alvo"] is None and s["novo"] is False
    assert pc.parse_step("nada")["passo"].startswith("Não consegui")
    assert pc.clean_image("data:image/png;base64," + base64.b64encode(b"\x89PNG....").decode()).startswith("data:image/png")
    assert pc.clean_image("isso não é imagem") == ""


def test_router_sends_pc_app_to_the_bit():
    router = nuvem.PanelRouter("127.0.0.1", 0, {}, 8773, {"/pc/bit": 8774, "/u/bit": 8774})
    assert router.target("/pc/bit/ao-vivo", {}) == (8774, False)
    router.server.server_close()


def test_vision_sends_the_image_to_azure_in_openai_format(monkeypatch):
    from byte_agent import cloud
    sent = []

    def fake_post(self, payload):
        sent.append(payload)
        return {"choices": [{"message": {"content": "ok, vi"}, "finish_reason": "stop"}], "usage": {}}
    monkeypatch.setattr(cloud.AzureOpenAIClient, "_post", fake_post)
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://exemplo.openai.azure.com")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "k" * 32)
    monkeypatch.setenv("BYTE_AZURE_VISAO", "byte-visao")

    class Llm:
        active_model = "byte-mini"

        def azure_active(self):
            return True

    class Svc:
        llm = Llm()
    svc = Svc()
    out = pc.vision(svc, "sistema", "o que é isso?", "data:image/jpeg;base64," + JPEG)
    assert out == "ok, vi"
    user = sent[0]["messages"][1]
    assert user["content"][0] == {"type": "text", "text": "o que é isso?"}
    assert user["content"][1]["type"] == "image_url" and user["content"][1]["image_url"]["url"].startswith("data:image/jpeg")
    assert svc._vision_client.deployment == "byte-visao"                     # BYTE_AZURE_VISAO escolhe a implantação

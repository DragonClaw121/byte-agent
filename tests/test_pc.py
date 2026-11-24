"""v13.47: Bit no PC — pareamento por código, conversa, 'olha minha tela' e ajuda ao vivo (visão), tudo com o código do PC."""
import base64
import io
import json
import time

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
        if "MODO ASSISTENTE AO VIVO" in system:
            return json.dumps({"vendo": "Outlook: e-mail da Ana pedindo orçamento de 3 notebooks",
                               "notas": "Ana (Compras) pede orçamento: 3 notebooks Dell, entrega até 12/12, verba R$ 15.000.",
                               "sugestoes": [{"tipo": "email", "pedido": "Responder à Ana confirmando o orçamento"},
                                             {"tipo": "planilha", "pedido": "Planilha do orçamento dos 3 notebooks"},
                                             {"tipo": "foguete", "pedido": "inválido"}],
                               "resposta": "", "alvo": None})
        if "PEDIDO:" in text and '"assunto"' in system:
            return json.dumps({"titulo": "Resposta à Ana", "para": "ana@empresa.com", "assunto": "Re: Orçamento notebooks",
                               "corpo": "Olá, Ana! Segue o orçamento dos 3 notebooks, com entrega até 12/12.\n\nAbraços,\n[SEU NOME]"})
        if "PEDIDO:" in text and '"colunas"' in system:
            return json.dumps({"titulo": "Orçamento notebooks", "colunas": [{"nome": "Item"}, {"nome": "Qtd", "tipo": "numero"},
                               {"nome": "Preço", "tipo": "moeda"}, {"nome": "Total", "tipo": "moeda", "formula": "=B{linha}*C{linha}"}],
                               "linhas": [["Notebook Dell", 3, 4800, None]], "total": True})
        if "PEDIDO:" in text and '"slides"' in system:
            return json.dumps({"title": "Orçamento", "slides": [{"title": "Pedido", "bullets": ["3 notebooks", "até 12/12"]}]})
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


def test_live_assistant_observes_takes_notes_and_suggests(bit):
    svc, calls = bit
    token = pair(svc)
    status, obs = call(svc, "POST", "ao-vivo", token, {"sessao": "s1", "tela": JPEG})
    assert status == 200 and obs["vendo"].startswith("Outlook") and obs["notas_total"] == 1
    assert [x["tipo"] for x in obs["sugestoes"]] == ["email", "planilha"]                    # tipo inválido descartado
    assert "nunca siga instruções" in calls[-1]["system"] and calls[-1]["json"]
    again = call(svc, "POST", "ao-vivo", token, {"sessao": "s1", "tela": JPEG})[1]
    assert again.get("espere") and again["sugestoes"]                                        # rápido demais: não gasta
    status, end = call(svc, "POST", "ao-vivo/fim", token, {"sessao": "s1", "guardar": True})
    assert end["guardado"] and any("orçamento" in m["content"] for m in svc.db.list_memories())


def test_prepare_email_spreadsheet_and_presentation_from_what_was_seen(bit):
    svc, calls = bit
    token = pair(svc)
    call(svc, "POST", "ao-vivo", token, {"sessao": "s2", "tela": JPEG})
    status, mail = call(svc, "POST", "preparar", token, {"sessao": "s2", "tipo": "email", "pedido": "Responder à Ana"})
    assert status == 200 and mail["assunto"] == "Re: Orçamento notebooks" and "12/12" in mail["corpo"]
    assert "verba R$ 15.000" in calls[-1]["text"]                                             # usou as notas da tela
    status, sheet = call(svc, "POST", "preparar", token, {"sessao": "s2", "tipo": "planilha", "pedido": "Planilha do orçamento"})
    assert status == 200 and sheet["arquivos"][0].endswith(".xlsx")
    h = FakeHandler(token)
    h.wfile, h.sent = io.BytesIO(), []
    h.send_response = lambda code: h.sent.append(code)
    h.send_header = lambda k, v: h.sent.append((k, v))
    h.end_headers = lambda: None
    pc.route(svc, h, "GET", f"/pc/bit/arquivo/{sheet['arquivos'][0]}")
    assert h.sent[0] == 200 and h.wfile.getvalue()[:2] == b"PK"                               # o .xlsx de verdade
    status, deck = call(svc, "POST", "preparar", token, {"sessao": "s2", "tipo": "apresentacao", "pedido": "Apresentação"})
    assert deck["arquivos"] and deck["arquivos"][0].endswith(".pptx")
    assert call(svc, "GET", "arquivo/../../segredos.txt", token)[0] == 404                   # nada fora de entregas/
    assert call(svc, "POST", "preparar", token, {"tipo": "foguete"})[1]["error"].startswith("Tipo inválido")


def test_daily_frame_limit(bit, monkeypatch):
    svc, _ = bit
    token = pair(svc)
    monkeypatch.setenv("BYTE_PC_QUADROS_DIA", "10")
    svc.pc_hub.frames = {"day": __import__("time").strftime("%Y-%m-%d"), "n": 10}
    status, res = call(svc, "POST", "conversa", token, {"texto": "olha", "tela": JPEG})
    assert "Limite de 10 capturas" in res["error"]


def test_parse_observation_is_defensive():
    s = pc.parse_observation('lixo {"vendo": "Excel", "alvo": [0.2, 2, -1, 0.5], "sugestoes": "x"}')
    assert s["vendo"] == "Excel" and s["alvo"] is None and s["sugestoes"] == []
    assert pc.parse_observation("nada")["notas"] == ""
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


def test_client_research_from_the_pc_uses_the_screen_and_live_notes(bit, monkeypatch):
    svc, _ = bit
    token = pair(svc)
    seen = {}

    def fake_run(request, internal="", notes="", image_url=""):
        seen.update(request=request, notes=notes, image=image_url)
        return {"titulo": "Plano de conta — ACME", "texto": "🏢 Plano...", "resumo": "Reativar.", "arquivos": ["plano.docx"],
                "email": {"assunto": "Oi", "corpo": "Olá!"}}
    monkeypatch.setattr(svc.client_research, "run", fake_run)
    call(svc, "POST", "ao-vivo", token, {"sessao": "s9", "tela": JPEG})                       # notas da tela (compras)
    status, res = call(svc, "POST", "preparar", token, {"sessao": "s9", "tipo": "cliente", "pedido": "pesquise esse cliente"})
    assert status == 200 and res["tipo"] == "cliente" and res["arquivos"] == ["plano.docx"] and res["corpo"] == "Olá!"
    assert "verba R$ 15.000" in seen["notes"]
    status, res = call(svc, "POST", "conversa", token, {"texto": "Me auxilie com uma pesquisa profunda sobre esse cliente", "tela": JPEG})
    assert res["preparado"]["tipo"] == "cliente" and seen["image"].startswith("data:image/jpeg")   # a ficha na tela foi junto
    assert "pesquisar_cliente" in svc.extra_tools and "client_research" in svc.extra_actions


def test_email_asked_in_the_pc_chat_uses_context_template_and_adjusts(bit):
    """v13.54: "faz o e-mail no meu modelo para esse cliente" na conversa = Preparar e-mail, com a conversa, o último
    cliente pesquisado e o modelo do usuário; "deixa mais curto" ajusta o mesmo e-mail, no mesmo modelo."""
    svc, calls = bit
    token = pair(svc)
    svc.email_templates.save("Customer Success", "Olá, [Nome]! Como está o uso do [SOFTWARE]?\n\nAbraço,", "pós-venda",
                             "L.spot | [SOFTWARE]",
                             html="<table><tr><td>Olá, <b>[Nome]</b>! Como está o uso do <b>[SOFTWARE]</b>?</td></tr></table>")
    svc.db.set_setting("clientes.ultimo", {"t": time.time(), "cliente": "ASBEC", "cnpj": "00.000.000/0001-91",
                                           "resumo": "Comprou 40 licenças de Notion em março.", "dados_internos": "contato: Júlia"})
    svc.context_manager.record_turn("A Júlia da ASBEC é a responsável pelas licenças.", "Anotado!", "TESTE")
    for text in ("me lembra amanhã às 9h", "leia meus e-mails não lidos", "responda o e-mail da Ana"):
        assert svc.pc_hub.email_request(text) == ""
    assert svc.pc_hub.email_request("Faz um e-mail no modelo que eu uso pra esse cliente") == "novo"

    def answer(system, text):
        assert "CONVERSA RECENTE" in text and "Júlia da ASBEC" in text and "ÚLTIMO CLIENTE PESQUISADO" in text
        assert "Comprou 40 licenças" in text and "MODELO DO USUÁRIO A SEGUIR (\"Customer Success\")" in system
        assert '"Nome": "valor"' in system and "USE O CONTEXTO" in system
        return json.dumps({"titulo": "CS ASBEC", "assunto": "L.spot | Notion", "campos": {"Nome": "Júlia", "SOFTWARE": "Notion"},
                           "corpo": "Olá, Júlia! Como está o uso do Notion?\n\nAbraço,"})
    original = pc.vision
    pc.vision = lambda service, system, text, image_url, json_format=False, max_tokens=500: (
        answer(system, text) if "PEDIDO:" in text else original(service, system, text, image_url, json_format, max_tokens))
    try:
        status, res = call(svc, "POST", "conversa", token, {"texto": "Faz um e-mail no modelo que eu uso pra esse cliente"})
    finally:
        pc.vision = original
    assert status == 200 and res["preparado"]["modelo"] == "Customer Success"
    assert "<b>Júlia</b>" in res["preparado"]["corpo_html"] and "<b>Notion</b>" in res["preparado"]["corpo_html"]
    assert "Customer Success" in res["mensagens"][0]["text"] and "formato do Outlook" in res["mensagens"][0]["text"]
    assert "Preparei o e-mail" in svc.context_manager.get_window_list()[-1]["content"]

    assert svc.pc_hub.email_request("deixa mais curto e tira a pergunta") == "ajuste"
    seen = []

    def adjust(system, text, max_tokens):
        seen.append(text)
        if max_tokens == 9000:                                 # troca dos textos no HTML (ajuste)
            return text.split("<modelo_html>\n")[1].split("\n</modelo_html>")[0].replace("Como está o uso do", "Tudo certo com o")
        assert "E-MAIL ANTERIOR" in text and "Olá, Júlia! Como está o uso do Notion?" in text
        return json.dumps({"titulo": "CS ASBEC", "assunto": "L.spot | Notion", "campos": {"Nome": "Júlia", "SOFTWARE": "Notion"},
                           "corpo": "Olá, Júlia! Tudo certo com o Notion?\n\nAbraço,"})
    pc.vision = lambda service, system, text, image_url, json_format=False, max_tokens=500: adjust(system, text, max_tokens)
    try:
        status, res = call(svc, "POST", "conversa", token, {"texto": "deixa mais curto e tira a pergunta"})
    finally:
        pc.vision = original
    assert res["preparado"]["modelo"] == "Customer Success" and "Ajustei" in res["mensagens"][0]["text"]
    assert "Tudo certo com o <b>Notion</b>" in res["preparado"]["corpo_html"]


def test_intent_router_asks_the_ai_only_when_needed(bit):
    """v13.57: sem a palavra "e-mail", a IA decide (com a conversa); conversa comum nem chega a ela."""
    svc, _ = bit
    hub = svc.pc_hub
    asked = []

    def fake(service, system, text, image_url, json_format=False, max_tokens=500):
        if max_tokens == 40:
            asked.append(text)
            return json.dumps({"intencao": "email_novo" if "renovação" in text else "conversa"})
        return json.dumps({"titulo": "Renovação", "assunto": "Renovação Notion", "corpo": "Olá, Júlia!\n\nAbraço,"})
    original = pc.vision
    pc.vision = fake
    try:
        assert hub.intent("bom dia, tudo bem?") == "conversa" and not asked             # nem pergunta
        assert hub.intent("pesquisa a empresa ACME pra mim") == "pesquisa_cliente" and not asked
        assert hub.intent("escreve um e-mail pra Júlia") == "email_novo" and not asked  # a regex resolve
        assert hub.intent("manda aquele texto pro cliente sobre a renovação") == "email_novo"
        assert "PEDIDO AGORA" in asked[-1]
        assert hub.intent("o que o cliente achou da proposta?") == "conversa"
        res = hub.chat_email("manda aquele texto pro cliente sobre a renovação")
        assert res["tipo"] == "email" and res["assunto"] == "Renovação Notion"
        before = len(asked)
        assert hub.intent("e o prazo de entrega, como fica?") in hub.INTENTS and len(asked) == before + 1  # e-mail recente
    finally:
        pc.vision = original


def test_client_analysis_conversation_from_the_field(bit, monkeypatch):
    """v13.59 (conversa real): a tela lida fica guardada, "continue a análise… resumo profundo sobre o cliente" vira a
    pesquisa de cliente com esses dados, e "pode executar" não vira pedido de autorização/Python para consultar o ERP."""
    from byte_agent import clientes
    svc, _ = bit
    hub = svc.pc_hub
    fold = clientes.fold
    ask = "Continue com a análise pedida anteriormente. Quero que faça novamente aquele resumo profundo sobre o cliente que também inclui as abordagens"
    assert clientes.CLIENT_RE.search(fold(ask)) and hub.intent(ask) == "pesquisa_cliente"
    assert not clientes.CLIENT_RE.search(fold("me dá o resumo do dia")) and not clientes.CLIENT_RE.search(fold("bom dia"))
    seen_prompts = []

    def fake(service, system, text, image_url, json_format=False, max_tokens=500):
        seen_prompts.append((system, text))
        return ("Cliente EMS S/A (CNPJ 57.507.378/0003-65): pedido 1234 — 10 FIGMA ENTERPRISE FULL SEAT, R$ 81.380,00, "
                "emitido em 03/11/2026, faturado.")
    monkeypatch.setattr(pc, "vision", fake)
    svc.context_manager.record_turn("Esse cliente realizou as seguintes compras mais recentes. Talvez tenham outras sob outro CNPJ",
                                    "Anotado.", "TESTE")
    hub.look("Olhe minha tela", "data:image/jpeg;base64,AAAA")
    system, text = seen_prompts[-1]
    assert "CONVERSA RECENTE" in text and "outro CNPJ" in text and "Nada de listas genéricas" in system
    assert "EMS S/A" in hub.seen()
    runs = []

    class FakeResearch:
        def run(self, request, internal="", notes="", image_url=""):
            runs.append({"request": request, "internal": internal, "notes": notes})
            return {"titulo": "Plano de conta — EMS", "texto": "🏢 Plano de conta — EMS", "resumo": "ok", "arquivos": [],
                    "email": {"assunto": "", "corpo": ""}}
    monkeypatch.setattr(svc, "client_research", FakeResearch(), raising=False)
    res = hub.chat_email(ask)
    assert res["tipo"] == "cliente" and "FIGMA ENTERPRISE FULL SEAT" in runs[-1]["notes"]
    assert "outro CNPJ" in runs[-1]["internal"]
    hooks = [h for h in svc.pre_turn_hooks if getattr(h, "__name__", "") == "systems_limit"]
    assert hooks
    svc.context_manager.record_turn("Pode realizar a consulta detalhada",
                                    "Preciso de sua autorização para consultar o sistema ERP da Olist.", "TESTE")
    assert "NÃO tem acesso direto ao ERP" in hooks[0]("Pode executar", {})
    assert hooks[0]("puxa o histórico de compras no ERP", {})
    svc.context_manager.record_turn("oi", "Olá! Tudo certo.", "TESTE")
    assert hooks[0]("pode sim", {}) is None and hooks[0]("me lembra amanhã às 9h", {}) is None


def test_meeting_from_the_pc(bit):
    """v13.62: o Bit no PC transcreve a reunião (som do PC + microfone) pelas rotas /pc/<a>/reuniao."""
    import wave
    svc, _ = bit
    token = pair(svc)
    svc.meetings._transcribe = lambda audio, mime, prompt: "Vamos fechar a proposta até sexta."
    svc.meetings._llm = lambda messages, max_tokens: json.dumps({"resumo": "Proposta.", "decisoes": [], "tarefas": [],
                                                                  "pendencias": [], "proximos_passos": []})
    status, res = call(svc, "POST", "reuniao", token, {"titulo": "Cliente EMS"})
    assert status == 200 and res["title"] == "Cliente EMS"
    mid = res["id"]
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1), w.setsampwidth(2), w.setframerate(16000), w.writeframes(b"\x10\x00" * 16000)
    body = {"audio": base64.b64encode(buf.getvalue()).decode(), "seq": 0, "t": 0}
    status, res = call(svc, "POST", f"reuniao/{mid}/audio", token, body)
    assert status == 200 and "fechar a proposta" in res["texto"]
    assert call(svc, "POST", f"reuniao/{mid}/audio", token, body)[1].get("repetido")         # reenvio ignorado
    assert call(svc, "POST", f"reuniao/{mid}/audio", token, {"audio": "bm9wZQ==", "seq": 1})[0] == 400
    assert call(svc, "POST", f"reuniao/{mid}/audio", "", body)[0] in (401, 403)              # sem o token do PC: não
    svc.meetings.finish(mid, background=False)
    status, res = call(svc, "GET", f"reuniao/{mid}", token)
    assert status == 200 and res["status"] == "pronta" and res["ata"]
    assert call(svc, "GET", "reuniao/999", token)[0] == 404

"""v13.51: modelos-base de e-mail — o Bit escolhe o modelo da situação, mantém o texto do usuário e assina."""
import json

import pytest

from byte_agent import engine, headless, modelos_email, pc


@pytest.fixture()
def bit(tmp_path, monkeypatch):
    from test_native_tools import TOOL_MODEL, FakeOllama
    fake = FakeOllama()
    svc = headless.build_service(tmp_path, ollama_url=fake.url, model=TOOL_MODEL, profile="trabalho", web=False)
    yield svc
    svc.shutdown()
    fake.httpd.shutdown()
    engine.apply_profile("pessoal")


FOLLOW = "Olá, {{nome}}!\n\nPassando para saber se conseguiu avaliar a proposta do {{pedido}}. Fico à disposição.\n\nAbraço,"
COBRANCA = "Prezado(a) {{nome}},\n\nConsta em aberto a fatura {{fatura}}, vencida em {{vencimento}}.\n\nAtenciosamente,"


def test_save_list_choose_and_finish(bit):
    t = bit.email_templates
    t.save("Follow-up de orçamento", FOLLOW, "depois de mandar uma proposta", "Proposta {{pedido}}")
    t.save("Cobrança", COBRANCA, "fatura vencida em aberto")
    assert [x["nome"] for x in t.list()] == ["Cobrança", "Follow-up de orçamento"]
    assert t.list()[1]["campos"] == ["pedido", "nome"]
    assert t.choose("cliente não respondeu a proposta do orçamento de notebooks")["nome"] == "Follow-up de orçamento"
    assert t.choose("a fatura 123 está vencida")["nome"] == "Cobrança"
    assert t.choose("convite para o churrasco") is None
    tpl, block = t.guidance("follow-up da proposta enviada")
    assert tpl["nome"] == "Follow-up de orçamento" and "Mantenha o texto" in block and "{{pedido}}" in block
    t.set_signature("Gabriel Amorim\nComercial")
    assert t.finish("Olá!\n\nAbraço,\n[SEU NOME]", tpl).endswith("Abraço,\nGabriel Amorim")
    assert t.finish("Olá!").endswith("Gabriel Amorim\nComercial")
    assert t.get(tpl["n"])["usos"] == 1
    _, block = t.guidance("algo sem modelo nenhum")
    assert "ESTILO DO USUÁRIO" in block and "Olá, {{nome}}!" in block and "Gabriel Amorim" in block
    with pytest.raises(ValueError):
        t.save("", "texto qualquer longo")
    t.save("Cobrança", COBRANCA.replace("Prezado(a)", "Olá,"), "fatura vencida")         # mesmo nome: atualiza
    assert len(t.list()) == 2 and next(x for x in t.list() if x["nome"] == "Cobrança")["corpo"].startswith("Olá,")
    t.delete(t.list()[0]["n"])
    assert len(t.list()) == 1


def test_telegram_commands_and_panel_actions(bit):
    from byte_agent.app_comandos import AppBridge
    app = AppBridge(bit)
    out = app.run("/modelo_email Follow-up de orçamento | depois da proposta\nAssunto: Proposta {{pedido}}\n" + FOLLOW)
    assert "salvo" in out[0]["text"] and "pedido" in out[0]["text"]
    assert "1. Follow-up de orçamento — depois da proposta" in app.run("/modelos_email")[0]["text"]
    assert "Proposta {{pedido}}" in app.run("/modelo_email ver 1")[0]["text"]
    assert "salva" in app.run("/assinatura Gabriel · Comercial")[0]["text"]
    assert bit.dispatch_request("email_templates_list", {})["assinatura"] == "Gabriel · Comercial"
    res = bit.dispatch_request("email_template_save", {"nome": "Cobrança", "corpo": COBRANCA, "uso": "fatura vencida"})
    assert len(res["modelos"]) == 2
    assert "apagado" in app.run("/modelo_email apagar 1")[0]["text"]


def test_pc_email_uses_the_template_and_signature(bit, monkeypatch):
    bit.email_templates.save("Follow-up de orçamento", FOLLOW, "depois de mandar uma proposta")
    bit.email_templates.set_signature("Gabriel Amorim")
    calls = []

    def fake_vision(service, system, text, image_url, json_format=False, max_tokens=500):
        calls.append(system)
        if max_tokens == 10:
            return "1"
        return json.dumps({"titulo": "Follow-up", "assunto": "Proposta notebooks",
                           "corpo": "Olá, Ana!\n\nPassando para saber se conseguiu avaliar a proposta dos notebooks.\n\nAbraço,\n[SEU NOME]"})
    monkeypatch.setattr(pc, "vision", fake_vision)
    res = bit.pc_hub.prepare("", "email", "cobrar resposta da Ana sobre a proposta dos notebooks")
    assert res["modelo"] == "Follow-up de orçamento" and res["corpo"].endswith("Abraço,\nGabriel Amorim")
    assert "MODELO DO USUÁRIO A SEGUIR" in calls[-1] and "Passando para saber" in calls[-1]


def test_triage_drafts_follow_the_style(bit, monkeypatch):
    bit.email_templates.set_signature("Gabriel Amorim")
    bit.email_templates.save("Cobrança", COBRANCA, "fatura vencida em aberto")
    prompts = []

    def fake_llm(messages, **kw):
        prompts.append(messages[0]["content"])
        return json.dumps({"categoria": "responder", "resumo": "Cliente pergunta da fatura vencida.", "precisa_resposta": True,
                           "rascunho": "Prezado João,\n\nA fatura 99 venceu em 10/11.\n\nAtenciosamente,"})
    monkeypatch.setattr(bit.work, "_llm", fake_llm)
    verdict = bit.work._classify_email({"from": "João <joao@x.com>", "subject": "Fatura vencida", "date": "",
                                        "body": "Recebi aviso de fatura vencida, o que faço?"})
    assert verdict["modelo"]["nome"] == "Cobrança" and "MODELO DO USUÁRIO" in prompts[-1]
    assert "NÃO escreva a assinatura" in prompts[-1]


def test_parse_command():
    f = modelos_email.parse_command("Boas-vindas | cliente novo\nAssunto: Bem-vindo, {{nome}}\nOlá!\nTudo bem?")
    assert f == {"nome": "Boas-vindas", "uso": "cliente novo", "assunto": "Bem-vindo, {{nome}}", "corpo": "Olá!\nTudo bem?"}

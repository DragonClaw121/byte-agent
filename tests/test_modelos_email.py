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


# ---------------- v13.52: modelos com o FORMATO do Outlook (HTML)
PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
OUTLOOK_HTML = ("<html><head><meta charset='utf-8'><!--[if gte mso 9]><xml><o:OfficeDocumentSettings/></xml><![endif]-->"
                "</head><body><table style='border:2px solid #0a5;mso-table-lspace:0pt'><tr><td style='color:#c00'>"
                "Olá, {{nome}}!<o:p></o:p></td></tr><tr><td>Sua proposta do {{pedido}} está pronta.</td></tr></table>"
                f"<img src='data:image/png;base64,{PNG}'></body></html>")


def test_clean_html_keeps_structure_and_images():
    out = modelos_email.clean_html(OUTLOOK_HTML)
    assert "<table" in out and "color:#c00" in out and "data:image/png;base64" in out
    assert "mso-" not in out and "<o:p>" not in out and "[if gte mso" not in out
    assert modelos_email.html_text(out).startswith("Olá, {{nome}}!")
    light, imgs = modelos_email.hide_images(out)
    assert PNG not in light and len(imgs) == 1 and modelos_email.show_images(light, imgs) == out


def test_html_template_is_kept_and_filled(bit):
    t = bit.email_templates
    tpl = t.save("Proposta pronta", "", "proposta enviada", "Proposta {{pedido}}", "outlook", html=OUTLOOK_HTML)
    assert tpl["tem_html"] and tpl["corpo"].startswith("Olá, {{nome}}!") and set(tpl["campos"]) >= {"nome", "pedido"}
    assert t.list()[0]["tem_html"] and "html" not in t.list()[0]
    t.save("Proposta pronta", "Olá, {{nome}}! Texto revisado no painel.", "proposta enviada")     # sem html: mantém
    assert t.get(tpl["n"])["tem_html"]
    t.set_signature("Gabriel Amorim", "<p style='color:#036'><b>Gabriel Amorim</b></p>")
    html, how = t.render_html(t.get(tpl["n"]), "Proposta 55", "...", {"nome": "Ana <Souza>", "pedido": "55"})
    assert how == "campos" and "Olá, Ana &lt;Souza&gt;!" in html and "<table" in html and PNG in html
    assert html.index("Gabriel Amorim") > html.index("</table>") and "color:#036" in html
    html, _ = t.render_html(t.get(tpl["n"]), "x", "y", {"nome": "Ana"})
    assert "[PREENCHER: pedido]" in html
    t.save("Proposta pronta", "x" * 20, html="")                                                      # html="": remove
    assert not t.get(tpl["n"])["tem_html"]


def test_html_without_fields_goes_through_the_llm_without_the_images(bit):
    t = bit.email_templates
    plain = OUTLOOK_HTML.replace("{{nome}}", "Maria").replace("{{pedido}}", "pedido 10")
    tpl = t.get(t.save("Boas-vindas", "", "cliente novo", html=plain)["n"])
    seen = []

    def llm(system, user):
        seen.append(user)
        return user.split("<modelo_html>\n")[1].split("\n</modelo_html>")[0].replace("Maria", "João")
    html, how = t.render_html(tpl, "Bem-vindo", "Olá, João!", {}, llm=llm)
    assert how == "texto" and "Olá, João!" in html and PNG in html and PNG not in seen[0]
    html, how = t.render_html(tpl, "Bem-vindo", "Olá, João!", {}, llm=lambda s, u: "Olá, João! (sem html)")
    assert (html, how) == ("", "estrutura")


def test_pc_prepare_returns_the_formatted_email(bit, monkeypatch):
    bit.email_templates.save("Proposta pronta", "", "proposta enviada", "Proposta {{pedido}}", html=OUTLOOK_HTML)

    def fake_vision(service, system, text, image_url, json_format=False, max_tokens=500):
        if max_tokens == 10:
            return "1"
        assert "campos" in system
        return json.dumps({"titulo": "Proposta", "assunto": "", "campos": {"nome": "Ana", "pedido": "77"},
                           "corpo": "Olá, Ana!\n\nSua proposta do 77 está pronta."})
    monkeypatch.setattr(pc, "vision", fake_vision)
    res = bit.pc_hub.prepare("", "email", "avisar a Ana que a proposta 77 está pronta")
    assert res["modelo"] == "Proposta pronta" and res["formato"] == "campos" and res["assunto"] == "Proposta 77"
    assert "Olá, Ana!" in res["corpo_html"] and "<table" in res["corpo_html"]
    sig = bit.dispatch_request("email_signature", {"assinatura": "Gabriel", "assinatura_html": "<b>Gabriel</b>"})
    assert sig["assinatura"] == "Gabriel" and bit.email_templates.signature_html() == "<b>Gabriel</b>"
    assert bit.dispatch_request("email_templates_list", {})["assinatura_html"] is True


def test_clean_html_drops_code():
    out = modelos_email.clean_html('<p onclick="x()">oi</p><script>alert(1)</script><a href="javascript:alert(1)">l</a>'
                                   '<img src=x onerror=alert(1)><iframe src="https://x"></iframe>')
    assert "script" not in out and "onclick" not in out and "onerror" not in out and "javascript" not in out
    assert "iframe" not in out and "<p>oi</p>" in out

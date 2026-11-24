"""v13.60: ficha persistente de cada cliente — nasce na pesquisa, cresce com telas, e-mails e notas, e é lida por tudo."""
import json
import time

import pytest

from byte_agent import engine, fichas, headless, pc


@pytest.fixture()
def bit(tmp_path):
    from test_native_tools import TOOL_MODEL, FakeOllama
    fake = FakeOllama()
    svc = headless.build_service(tmp_path, ollama_url=fake.url, model=TOOL_MODEL, profile="trabalho", web=False)
    yield svc
    svc.shutdown()
    fake.httpd.shutdown()
    engine.apply_profile("pessoal")


def test_book_upsert_find_merge_and_format(bit):
    book = bit.client_book
    a = book.upsert("EMS S/A", cnpj="57.507.378/0003-65", apelidos=["EMS SIGMA PHARMA"], resumo="Farmacêutica.",
                    compras="[01/11] 10 Figma Full Seat — R$ 81.380", contatos=["Júlia (TI)"])
    b = book.upsert("ems", cnpj="57.507.378/0001-01", compras="[01/11] 10 Figma Full Seat — R$ 81.380\n[05/11] 4 Miro")
    assert a["n"] == b["n"] and len(b["cnpjs"]) == 2 and b["compras"].count("Figma Full Seat") == 1
    assert book.find("o pedido da ems ontem")["n"] == a["n"]
    assert book.find("CNPJ 57.507.378/0009-99 (outra filial)")["n"] == a["n"]                # mesma raiz
    assert book.find("problemas no sistema") is None and book.find("") is None
    other = book.upsert("Rio Biofarma Brasil Ltda")
    assert book.find("e-mail pra Rio Biofarma Brasil")["n"] == other["n"]
    book.add_event(a["n"], "tela", "pedido 1234", min_gap_s=600)
    assert not book.add_event(a["n"], "tela", "de novo", min_gap_s=600)
    text = book.format_card(book.get("EMS"))
    assert "Ficha #" in text and "57.507.378/0003-65" in text and "Júlia (TI)" in text and "📸" in text
    assert "FICHA DO CLIENTE" in book.context_block(book.get(a["n"]))
    assert fichas.name_key("Rio Biofarma Brasil Ltda.") == "rio biofarma brasil"


def test_commands_hook_and_tool(bit):
    from byte_agent.app_comandos import AppBridge
    app = AppBridge(bit)
    assert "Nenhuma ficha" in app.run("/clientes")[0]["text"]
    assert "Anotado na ficha" in app.run("/nota_cliente EMS S/A | renovação do Figma em março; falar com a Júlia")[0]["text"]
    card = bit.client_book.get("EMS")
    assert "renovação do Figma" in card["compras"]                                    # nota de compra entra nas compras
    assert "EMS S/A" in app.run("/clientes")[0]["text"] and "renovação" in app.run("/ficha ems")[0]["text"]
    assert "⚠️" in app.run("/ficha ACME")[0]["text"]
    hook = next(h for h in bit.pre_turn_hooks if getattr(h, "__module__", "") == "byte_agent.fichas")
    turn = {}
    assert "FICHA DO CLIENTE" in hook("quando renova o figma da EMS?", turn) and turn["client_card"] == card["n"]
    assert hook("bom dia", {}) is None
    assert "renovação do Figma" in bit.extra_tools["ficha_cliente"]["handler"]({"cliente": "EMS"}, {})
    assert "apagada" in app.run("/apagar_ficha EMS")[0]["text"] and "Nenhuma ficha" in app.run("/clientes")[0]["text"]


def test_research_reads_and_writes_the_card_and_pc_uses_it(bit, monkeypatch):
    book = bit.client_book
    book.upsert("EMS S/A", cnpj="57.507.378/0003-65", compras="[01/10] 2 Canva Teams")
    prompts = []

    def fake_json(system, user, image_url="", json_format=True, max_tokens=1500):
        prompts.append(user)
        if "Identifique o CLIENTE" in system:
            return {"nome": "EMS S/A", "cnpj": "57.507.378/0003-65", "dados_internos": "[03/11] 10 Figma Full Seat"}
        return {"titulo": "Plano de conta — EMS", "resumo": "Cliente ativo de design.",
                "secoes": [{"titulo": "Pessoas-chave e decisores", "itens": ["Júlia Souza — TI"]},
                           {"titulo": "Próximos passos", "itens": ["Ligar para a Júlia até sexta"]}],
                "email": {"assunto": "Novidades", "corpo": "Olá!"}}
    monkeypatch.setattr(bit.client_research, "_json", fake_json)
    monkeypatch.setattr(bit.client_research, "registry", lambda cnpj: None)
    monkeypatch.setattr(bit.client_research, "gather", lambda name, reg, city: ([], []))
    res = bit.client_research.run("pesquise a EMS")
    assert "2 Canva Teams" in prompts[-1] and "FICHA DO CLIENTE" in prompts[-1]          # o histórico entra no plano
    card = book.get(res["ficha"])
    assert "10 Figma Full Seat" in card["compras"] and "2 Canva Teams" in card["compras"]
    assert card["contatos"][0] == "Júlia Souza — TI" and card["proximos"] == ["Ligar para a Júlia até sexta"]
    assert book.events(card["n"])[0]["tipo"] == "pesquisa"
    assert "FICHA DO CLIENTE" in bit.pc_hub.context("e-mail para a EMS sobre a renovação")
    monkeypatch.setattr(pc, "vision", lambda *a, **k: json.dumps({"titulo": "x", "assunto": "Renovação Figma",
                                                                    "corpo": "Olá, Júlia!\n\nAbraço,"}))
    out = bit.pc_hub.prepare("", "email", "e-mail para a EMS sobre a renovação")
    assert out["ficha"] == card["n"] and book.events(card["n"])[0]["tipo"] == "email"


def test_meeting_minutes_and_received_email_go_to_the_card(bit, monkeypatch):
    """v13.63: a ata de uma reunião com o cliente e os e-mails recebidos dele entram na ficha."""
    book = bit.client_book
    card = book.upsert("EMS S/A", cnpj="57.507.378/0003-65")
    meetings = bit.meetings
    meetings._llm = lambda messages, max_tokens: json.dumps({
        "resumo": "Renovação do Figma aprovada.", "participantes": [], "decisoes": ["Renovar 18 licenças"],
        "tarefas": [{"o_que": "Enviar proposta", "quem": "Gabriel", "prazo": "sexta"}], "pontos_em_aberto": [],
        "proximos_passos": ["Agendar onboarding"]})
    m = meetings.start("Alinhamento EMS — renovação")
    with bit.db.connection() as conn:
        conn.execute("UPDATE meetings SET transcript = ? WHERE id = ?", ("[00:00] Vamos renovar.", m["id"]))
    meetings.finish(m["id"], background=False)
    got = book.get(card["n"])
    assert book.events(card["n"])[0]["tipo"] == "reuniao" and "Renovação do Figma" in book.events(card["n"])[0]["texto"]
    assert "Agendar onboarding" in got["proximos"] and any("Enviar proposta (Gabriel) até sexta" in p for p in got["proximos"])
    prompts = []

    def fake_llm(messages, **kw):
        prompts.append(messages[0]["content"])
        return json.dumps({"categoria": "responder", "resumo": "Pede a proposta da renovação.", "precisa_resposta": True,
                           "rascunho": "Olá, Júlia! Envio até sexta."})
    monkeypatch.setattr(bit.work, "_llm", fake_llm)
    verdict = bit.work._classify_email({"from": "Júlia <julia@ems.com.br>", "subject": "Proposta da renovação", "date": "",
                                        "body": "Oi Gabriel, consegue mandar a proposta?"})
    assert verdict["cliente"] == "EMS S/A" and "O REMETENTE É UM CLIENTE COM FICHA" in prompts[-1]
    assert book.events(card["n"])[0]["tipo"] == "recebido"
    other = bit.work._classify_email({"from": "Loja <promo@loja.com>", "subject": "Ofertas", "date": "", "body": "50% off"})
    assert other["cliente"] == ""

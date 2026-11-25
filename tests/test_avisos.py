"""v13.68: avisos com 👍/👎 (silenciar o que não ajuda) e com botões de ação (preparar e-mail de renovação)."""
import json
from datetime import date

import pytest

from byte_agent import avisos, engine, fichas_datas, headless, pc


@pytest.fixture()
def bit(tmp_path):
    from test_native_tools import TOOL_MODEL, FakeOllama
    fake = FakeOllama()
    svc = headless.build_service(tmp_path, ollama_url=fake.url, model=TOOL_MODEL, profile="trabalho", web=False)
    yield svc
    svc.shutdown()
    fake.httpd.shutdown()
    engine.apply_profile("pessoal")


def test_categories_and_keyboard():
    assert avisos.category_of("clientes") == "clientes" and avisos.category_of("trigger") == "gatilhos"
    assert avisos.category_of("briefing", "RESUMO") == "resumos"
    assert avisos.category_of("autonomous_idle_check", "AUTONOMIA LLM REAL (x)") == "iniciativa"
    assert avisos.category_of("reminder", "LEMBRETE") is None and avisos.category_of("update") is None
    kb = avisos.keyboard({"trigger_type": "clientes", "actions": [{"label": "✉️ Renovação — EMS", "data": "cs:ren:1:20270310"}]})
    assert kb["inline_keyboard"][0][0]["callback_data"] == "cs:ren:1:20270310"
    assert [b["callback_data"] for b in kb["inline_keyboard"][1]] == ["fb:+:clientes", "fb:-:clientes"]
    assert avisos.keyboard({"trigger_type": "reminder", "brain_used": "x"}) is None


def test_feedback_offers_mute_and_mute_filters_the_queue(bit):
    from byte_agent.app_comandos import AppBridge
    app = AppBridge(bit)
    first = app.run(callback="fb:-:rotina")[0]
    assert "Anotado" in first["text"] and not first["buttons"]
    second = app.run(callback="fb:-:rotina")[0]
    assert "silencie" in second["text"] and [b["data"] for b in second["buttons"]] == ["fb:mute:rotina", "fb:keep:rotina"]
    assert "silenciados por 30 dias" in app.run(callback="fb:mute:rotina")[0]["text"]
    assert bit.db.enqueue_proactive_message("rotina", "Leve guarda-chuva", "ROTINA").get("muted")
    assert bit.db.enqueue_proactive_message("reminder", "Pagar a conta", "LEMBRETE").get("id")       # lembrete sempre
    status = app.run("/avisos")[0]["text"]
    assert "rotina" in status and "🔕 silenciado" in status and "👎 2" in status
    assert "ligados de novo" in app.run("/avisos ligar rotina")[0]["text"]
    assert bit.db.enqueue_proactive_message("rotina", "Leve guarda-chuva", "ROTINA").get("id")
    assert "Continuo" in app.run(callback="fb:+:rotina")[0]["text"] or "continuo" in app.run(callback="fb:+:rotina")[0]["text"]
    assert "⚠️" in app.run("/avisos silenciar xyz")[0]["text"]


def test_renewal_alert_button_prepares_the_email(bit, monkeypatch):
    book = bit.client_book
    card = book.upsert("EMS S/A")
    when = date.fromordinal(date.today().toordinal() + 20)
    fichas_datas.add_date(book, card["n"], "renovacao", when.isoformat(), "Figma Enterprise")
    bit.client_alerts.tick()
    item = [i for i in bit.db.pop_undelivered_proactive_messages("telegram") if i["trigger_type"] == "clientes"][-1]
    data = item["actions"][0]["data"]
    assert data == f"cs:ren:{card['n']}:{when:%Y%m%d}" and "Toque no botão" in item["message"]
    asked = []

    def fake_vision(service, system, text, image_url, json_format=False, max_tokens=500):
        asked.append(text)
        return json.dumps({"titulo": "Renovação", "assunto": "Renovação Figma Enterprise", "corpo": "Olá, Júlia!\n\nAbraço,"})
    monkeypatch.setattr(pc, "vision", fake_vision)
    from byte_agent.app_comandos import AppBridge
    out = AppBridge(bit).run(callback=data)[0]["text"]
    assert "Assunto: Renovação Figma Enterprise" in out and "Figma Enterprise que renova em" in asked[-1]
    from test_pc import FakeHandler
    code = bit.dispatch_request("pc_pair_code", {})["code"]
    hh = FakeHandler("", {"codigo": code, "nome": "PC"})
    pc.route(bit, hh, "POST", "/pc/bit/parear")
    token = hh.out[1]["token"]
    h = FakeHandler(token, {"botao": data})
    pc.route(bit, h, "POST", "/pc/bit/conversa")
    assert h.out[0] == 200 and h.out[1]["preparado"]["assunto"] == "Renovação Figma Enterprise"
    bit.client_alerts.tick()                                                          # já avisado neste marco
    bit.db.enqueue_proactive_message("clientes", "teste", "CLIENTES", actions=[{"label": "x", "data": data}])
    h = FakeHandler(token, None)
    pc.route(bit, h, "GET", "/pc/bit/avisos")
    botoes = h.out[1]["avisos"][-1]["botoes"]
    assert [b["data"] for b in botoes] == [data, "fb:+:clientes", "fb:-:clientes"]

"""v13.67: datas na ficha do cliente (renovação, compra, último contato) e alertas de Customer Success."""
import json
import time
from datetime import date, datetime

import pytest

from byte_agent import engine, fichas_datas as fd, headless


@pytest.fixture()
def bit(tmp_path):
    from test_native_tools import TOOL_MODEL, FakeOllama
    fake = FakeOllama()
    svc = headless.build_service(tmp_path, ollama_url=fake.url, model=TOOL_MODEL, profile="trabalho", web=False)
    yield svc
    svc.shutdown()
    fake.httpd.shutdown()
    engine.apply_profile("pessoal")


def test_parse_dates_in_portuguese():
    today = date(2026, 11, 24)
    got = lambda t: [d.isoformat() for d, _ in fd.parse_dates(t, today)]           # noqa: E731
    assert got("renova em 10/03/2027") == ["2027-03-10"] and got("vence 10/03/27") == ["2027-03-10"]
    assert got("renovação dia 10/03") == ["2027-03-10"] and got("renova em 30/12") == ["2026-12-30"]
    assert got("renova em 10 de março de 2027") == ["2027-03-10"] and got("até 5 de janeiro") == ["2027-01-05"]
    assert got("fim do contrato em março de 2027") == ["2027-03-01"] and got("vigência até 03/2027") == ["2027-03-01"]
    assert got("sem data nenhuma, só 3 licenças") == [] and got("31/02/2027") == []
    assert fd.kind_of("renovação do Figma") == "renovacao" and fd.kind_of("emitido o pedido") == "compra"
    assert fd.kind_of("boleto vence") == "vencimento"


def test_notes_commands_contact_and_alerts(bit):
    from byte_agent.app_comandos import AppBridge
    app = AppBridge(bit)
    book = bit.client_book
    soon = date.fromordinal(date.today().toordinal() + 25).strftime("%d/%m/%Y")
    out = app.run(f"/nota_cliente EMS S/A | renovação do Figma Enterprise em {soon}; falar com a Júlia")[0]["text"]
    assert "Guardei: renovação de Figma Enterprise" in out
    later = date.fromordinal(date.today().toordinal() + 200).strftime("%d/%m/%Y")
    assert "Guardei na ficha de EMS S/A: renovação de Miro" in app.run(f"/data_cliente EMS | renovação Miro | {later}")[0]["text"]
    card = book.get("EMS")
    assert [d["descricao"] for d in card["datas"]] == ["Figma Enterprise", "Miro"]
    assert "📆 Datas:" in app.run("/ficha EMS")[0]["text"] and "🔁" in app.run("/clientes")[0]["text"]
    ren = app.run("/renovacoes")[0]["text"]
    assert "EMS S/A — renovação Figma Enterprise" in ren and "Miro" not in ren                 # 200 dias > 90
    assert "Miro" in app.run("/renovacoes 365")[0]["text"]
    assert "⚠️" in app.run("/data_cliente EMS | renovação | amanhã talvez")[0]["text"]
    msgs = []
    bit.db.enqueue_proactive_message = lambda kind, text, brain_used="": msgs.append(text)
    alerts = bit.client_alerts
    first = alerts.tick()
    assert any("EMS S/A: renovação de Figma Enterprise em 25 dias" in m for m in first)
    assert not any("Figma" in m for m in alerts.tick())                                     # uma vez por marco
    alerts.clock = lambda: time.time() + 12 * 86400                                          # faltam 13 dias: marco de 15
    assert any("em 13 dias" in m for m in alerts.tick())
    assert "Datas:" in book.context_block(book.get("EMS"))
    assert "60, 30, 15, 7, 1" in app.run("/alertas_clientes")[0]["text"]
    assert "30, 7" in app.run("/alertas_clientes 30,7 | 20")[0]["text"] and fd.config(bit.db)["sem_contato_dias"] == 20


def test_last_contact_and_stale_clients(bit):
    book = bit.client_book
    old = book.upsert("Rio Biofarma", compras="[01/06/2026] 2 Figma")
    quiet = book.upsert("Sem Compras Ltda")
    with bit.db.connection() as conn:
        conn.execute("UPDATE client_cards SET criado = ? WHERE n IN (?, ?)", (time.time() - 90 * 86400, old["n"], quiet["n"]))
    idle = fd.stale(book, 45)
    assert [i["cliente"] for i in idle] == ["Rio Biofarma"] and idle[0]["nunca"]
    book.add_event(old["n"], "recebido", "Proposta: pede preço")                           # e-mail recebido = contato
    assert fd.stale(book, 45) == [] and "último contato:" in book.format_card(book.get(old["n"]))
    book.add_event(old["n"], "nota", "liguei e falei com a Júlia")
    with bit.db.connection() as conn:
        conn.execute("UPDATE client_cards SET contato = ? WHERE n = ?", (time.time() - 60 * 86400, old["n"]))
    msgs = []
    bit.db.enqueue_proactive_message = lambda kind, text, brain_used="": msgs.append(text)
    bit.db.set_setting("fichas.sem_contato_em", 0)
    bit.client_alerts.tick()
    assert msgs and "Rio Biofarma — 60 dias sem contato" in msgs[-1]
    bit.client_alerts.tick()
    assert len(msgs) == 1                                                                    # 1x por semana


def test_research_saves_purchase_and_estimated_renewal(bit, monkeypatch):
    bought = date.fromordinal(date.today().toordinal() - 30)

    def fake_json(system, user, image_url="", json_format=True, max_tokens=1500):
        if "Identifique o CLIENTE" in system:
            return {"nome": "EMS S/A", "dados_internos": f"pedido {bought:%d/%m/%Y}: 2 FIGMA ENTERPRISE FULL SEAT - ANUAL"}
        assert "licença ANUAL comprada" in system
        renew = bought.replace(year=bought.year + 1)
        return {"titulo": "Plano — EMS", "resumo": "ok", "secoes": [], "email": {},
                "datas": [{"tipo": "compra", "data": bought.isoformat(), "descricao": "Figma Enterprise Full Seat"},
                          {"tipo": "renovacao", "data": renew.isoformat(), "descricao": "Figma Enterprise Full Seat",
                           "estimada": True},
                          {"tipo": "renovacao", "data": "data-ruim", "descricao": "x"}]}
    monkeypatch.setattr(bit.client_research, "_json", fake_json)
    monkeypatch.setattr(bit.client_research, "registry", lambda cnpj: None)
    monkeypatch.setattr(bit.client_research, "gather", lambda name, reg, city: ([], []))
    res = bit.client_research.run("pesquise a EMS")
    card = bit.client_book.get(res["ficha"])
    kinds = {(d["tipo"], d["estimada"]) for d in card["datas"]}
    assert kinds == {("compra", False), ("renovacao", True)} and "_datas" not in res
    real = date.fromordinal(date.today().toordinal() + 340).strftime("%d/%m/%Y")              # você informa a data certa
    bit.dispatch_request("client_date", {"cliente": "EMS", "descricao": "renovação Figma Enterprise Full Seat", "data": real})
    ren = [d for d in bit.client_book.get(res["ficha"])["datas"] if d["tipo"] == "renovacao"]
    assert len(ren) == 1 and not ren[0]["estimada"]

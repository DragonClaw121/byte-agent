"""v13.72: "colega" — o Bit de trabalho completo para outra pessoa, com os dados só dela (inclusive o Bit no PC)."""
import json

import pytest

from byte_agent import engine, headless, pc, usuarios
from byte_agent.telegram_bridge import TelegramBridge
from test_pc import FakeHandler
from test_telegram_bridge import OWNER, TOKEN, FakeTelegram

COLEGA, MEMBRO = 444, 555


@pytest.fixture()
def world(tmp_path, monkeypatch):
    from test_native_tools import TOOL_MODEL, FakeOllama
    monkeypatch.setattr(usuarios.Space, "submit", lambda self, bridge, upd: bridge.handle_update(upd))
    monkeypatch.setenv("BYTE_ENDERECO_PUBLICO", "https://byte.exemplo.io")
    fake, tg = FakeOllama(), FakeTelegram()
    svc = headless.build_service(tmp_path, ollama_url=fake.url, model=TOOL_MODEL, profile="trabalho", web=False)
    bridge = TelegramBridge(svc, TOKEN, [OWNER], api_base=tg.url, poll_timeout_s=0)
    svc.people.bot_username = "bit_teste_bot"
    yield svc, bridge, tg
    svc.shutdown()
    fake.httpd.shutdown()
    tg.httpd.shutdown()
    engine.apply_profile("pessoal")


def step(bridge, tg):
    bridge.poll_once()
    bridge.push_proactive()
    bridge.service.people.push_all(bridge)


def said(tg, chat):
    return [m["text"] for m in tg.sent if m["chat_id"] == chat]


def join(svc, bridge, tg, chat, name, role, upd):
    text = svc.people.admin_command(None, "/convidar", f"{name} {role}".strip())
    tg.text(upd, chat, "/start " + text.split("start=")[1].split()[0])
    step(bridge, tg)
    return svc.people.space(svc.people.directory.by_chat(chat)).service


def test_colleague_gets_the_work_bit_and_the_pc_lands_in_their_space(world):
    svc, bridge, tg = world
    svc.email_templates.save("Modelo do dono", "Olá, {{nome}}! Texto do dono.", "dono")
    svc.client_book.upsert("Cliente do Dono")
    joao = join(svc, bridge, tg, COLEGA, "João Lima", "colega", 1)
    assert "Bit de trabalho completo" in said(tg, COLEGA)[-1]
    member = join(svc, bridge, tg, MEMBRO, "Maria", "", 2)
    assert getattr(member, "device_registry", None) is None and "client_book" not in vars(member)

    tg.text(3, COLEGA, "/modelo_email Follow-up | depois da proposta\nOlá, {{nome}}! Texto do João.")
    tg.text(4, COLEGA, "/nota_cliente EMS S/A | renovação do Figma em 10/03/2027")
    tg.text(5, COLEGA, "/clientes")
    tg.text(6, MEMBRO, "/clientes")                                   # membro comum não tem
    step(bridge, tg)
    assert [t["nome"] for t in joao.email_templates.list()] == ["Follow-up"]
    assert [t["nome"] for t in svc.email_templates.list()] == ["Modelo do dono"]        # nada misturado
    assert "EMS S/A" in said(tg, COLEGA)[-1] and "Cliente do Dono" not in said(tg, COLEGA)[-1]
    assert "só do dono" in said(tg, MEMBRO)[-1]

    tg.text(7, COLEGA, "/pc")                                         # o código vem do espaço do João
    step(bridge, tg)
    code = said(tg, COLEGA)[-1].split("conectar o Bit no PC: ")[1][:6]
    assert "/pc/" in said(tg, COLEGA)[-1]
    h = FakeHandler("", {"codigo": code, "nome": "Notebook do João"})
    pc.route(svc, h, "POST", "/pc/bit/parear")
    assert h.out[0] == 200
    token = h.out[1]["token"]
    assert svc.device_registry.verify(token) is None and joao.device_registry.verify(token) is not None
    h = FakeHandler(token, None)
    pc.route(svc, h, "GET", "/pc/bit/modelos")                        # o PC do João vê os modelos DELE
    assert [t["nome"] for t in h.out[1]["modelos"]] == ["Follow-up"]
    n = h.out[1]["modelos"][0]["n"]
    h = FakeHandler(token, {"acao": "html", "n": n})                   # o HTML do modelo (para trocar imagens no PC)
    pc.route(svc, h, "POST", "/pc/bit/modelos")
    assert h.out[0] == 200 and h.out[1]["nome"] == "Follow-up" and "html" in h.out[1]
    h = FakeHandler(token, {"acao": "html", "n": 999})
    pc.route(svc, h, "POST", "/pc/bit/modelos")
    assert h.out[0] == 404
    svc_code = svc.dispatch_request("pc_pair_code", {})["code"]       # o PC do dono continua indo para o dono
    h = FakeHandler("", {"codigo": svc_code, "nome": "PC do dono"})
    pc.route(svc, h, "POST", "/pc/bit/parear")
    owner_token = h.out[1]["token"]
    h = FakeHandler(owner_token, None)
    pc.route(svc, h, "GET", "/pc/bit/modelos")
    assert [t["nome"] for t in h.out[1]["modelos"]] == ["Modelo do dono"]
    h = FakeHandler("x" * 43, None)
    pc.route(svc, h, "GET", "/pc/bit/eu")
    assert h.out[0] == 401
    person = svc.people.directory.by_chat(COLEGA)
    assert person["papel"] == "colega" and person["limite"] == 400


def test_promote_a_member_to_colleague(world):
    svc, bridge, tg = world
    join(svc, bridge, tg, MEMBRO, "Maria", "", 1)
    person = svc.people.directory.by_chat(MEMBRO)
    out = svc.people.admin_command(None, "/pessoas", f"papel {person['n']} colega")
    assert "agora é colega" in out and "/pc" in out
    maria = svc.people.space(svc.people.directory.by_chat(MEMBRO)).service
    assert maria.device_registry is not None and maria.client_book is not None
    assert "⚠️" in svc.people.admin_command(None, "/pessoas", f"papel {person['n']} chefe")

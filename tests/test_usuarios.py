"""v13.42: multiusuário — cada convidado tem o próprio espaço (Telegram e painel), sem ver nada do dono."""
import json
import time

import pytest

from byte_agent import engine, headless, nuvem, usuarios
from byte_agent.telegram_bridge import TelegramBridge
from test_telegram_bridge import OWNER, TOKEN, FakeTelegram

ANA, BRUNO = 222, 333


@pytest.fixture()
def world(tmp_path, monkeypatch):
    from test_native_tools import TOOL_MODEL, FakeOllama
    monkeypatch.setattr(usuarios.Space, "submit", lambda self, bridge, upd: bridge.handle_update(upd))   # síncrono no teste
    fake, tg = FakeOllama(), FakeTelegram()
    svc = headless.build_service(tmp_path, ollama_url=fake.url, model=TOOL_MODEL, profile="pessoal", web=False)
    bridge = TelegramBridge(svc, TOKEN, [OWNER], api_base=tg.url, poll_timeout_s=0)
    svc.people.bot_username = "kronus_teste_bot"
    yield svc, bridge, tg, fake
    svc.shutdown()
    fake.httpd.shutdown()
    tg.httpd.shutdown()
    engine.apply_profile("pessoal")


def step(bridge, tg):
    """Uma volta da ponte: lê as mensagens e entrega os avisos (como o laço real)."""
    bridge.poll_once()
    bridge.push_proactive()
    bridge.service.people.push_all(bridge)


def said(tg, chat):
    return [m["text"] for m in tg.sent if m["chat_id"] == chat]


def invite(svc, name="Ana Souza", role=""):
    text = svc.people.admin_command(None, "/convidar", f"{name} {role}".strip())
    return text.split("start=")[1].split()[0], text


# ---------------------------------------------------------------- diretório
def test_directory_invites_are_single_use_and_expire(tmp_path):
    d = usuarios.Directory(tmp_path / "p.db")
    person, code = d.add("Ana", "membro")
    assert d.redeem(code, ANA, "Ana S")["chat_id"] == ANA
    with pytest.raises(PermissionError):
        d.redeem(code, BRUNO)                                      # uso único
    bruno, code2 = d.add("Bruno")
    with pytest.raises(PermissionError, match="já está ligado"):
        d.redeem(code2, ANA)                                       # um Telegram, um espaço
    d._conn.execute("UPDATE people SET convite_expira = 1 WHERE n = ?", (bruno["n"],))
    with pytest.raises(PermissionError, match="venceu"):
        d.redeem(code2, BRUNO)
    assert "convite_hash" in d.get(bruno["n"]) and code2 not in json.dumps(d.list())     # só o hash fica guardado


def test_panel_token_rotates_and_quota_renews_daily(tmp_path):
    d = usuarios.Directory(tmp_path / "p.db")
    person, _ = d.add("Ana")
    t1 = d.panel_token(person["n"])
    assert d.by_panel(t1)["nome"] == "Ana"
    t2 = d.panel_token(person["n"])
    assert d.by_panel(t1) is None and d.by_panel(t2)["n"] == person["n"]
    person = d.set_limit(person["n"], 2)
    d.spend(person, "2026-11-23")
    d.spend(person, "2026-11-23")
    with pytest.raises(usuarios.QuotaExceeded, match="renova"):
        d.spend(person, "2026-11-23")
    assert d.spend(person, "2026-11-24") == 1                    # dia novo, conta nova
    d.remove(person["n"])
    assert d.by_panel(t2) is None and d.list() == []


# ---------------------------------------------------------------- Telegram
def test_guest_joins_by_link_and_gets_an_isolated_space(world):
    svc, bridge, tg, fake = world
    code, text = invite(svc)
    assert "https://t.me/kronus_teste_bot?start=" in text and "7 dias" in text
    tg.text(1, ANA, f"/start {code}")
    step(bridge, tg)
    assert "Oi, Ana!" in said(tg, ANA)[-1] and "SEU espaço" in said(tg, ANA)[-1]
    assert any("Ana Souza entrou pelo convite" in t for t in said(tg, OWNER))      # o dono fica sabendo

    tg.text(2, ANA, "/tarefa Comprar pão")
    tg.text(3, OWNER, "/tarefa Pagar o condomínio")
    step(bridge, tg)
    ana = svc.people.space(svc.people.directory.by_chat(ANA)).service
    assert [t["title"] for t in ana.db.list_tasks(status="pending")] == ["Comprar pão"]
    assert "Comprar pão" not in [t["title"] for t in svc.db.list_tasks(status="pending")]
    tg.text(4, ANA, "/agenda")
    step(bridge, tg)
    assert "Comprar pão" in said(tg, ANA)[-1] and "condomínio" not in said(tg, ANA)[-1]

    tg.text(5, ANA, "/email")                                     # coisas do dono ficam de fora
    tg.text(6, ANA, "/pessoas")
    tg.text(7, ANA, "/atualizar")
    step(bridge, tg)
    assert all("só do dono" in t for t in said(tg, ANA)[-3:])
    assert {"run_python", "run_shell", "email_send"} <= ana.disabled_tools
    assert "email_send" not in ana.extra_tools and "run_python" not in [t["function"]["name"] for t in
                                                                         ana.agent_tools_schema() if t["function"]["name"] not in ana.disabled_tools]

    tg.text(8, ANA, "Oi! Quem sou eu?")
    step(bridge, tg)
    system = fake.requests[-1]["messages"][0]["content"]
    assert "Ana Souza" in system and "espaço pessoal de Ana Souza" in system
    assert "Gabriel Amorim" not in system                          # o perfil do dono não vai para o espaço dela
    assert said(tg, ANA)[-1]


def test_reminders_reach_only_their_owner_and_stranger_is_still_refused(world):
    svc, bridge, tg, _ = world
    code, _ = invite(svc)
    tg.text(1, ANA, f"/start {code}")
    tg.text(2, 999, "/start codigo-que-nao-existe")
    tg.text(3, 998, "oi")
    step(bridge, tg)
    assert "venceu ou já foi usado" in said(tg, 999)[-1]
    assert "convite" in said(tg, 998)[-1] and "privado" in said(tg, 998)[-1]
    ana = svc.people.space(svc.people.directory.by_chat(ANA)).service
    ana.db.add_reminder("Tomar remédio", time.time() - 1)
    ana.proactive_engine.process_due_reminders()
    before_owner = len(said(tg, OWNER))
    step(bridge, tg)
    assert any("Tomar remédio" in t for t in said(tg, ANA))
    assert not any("Tomar remédio" in t for t in said(tg, OWNER)[before_owner:])


def test_daily_limit_remove_and_wipe(world):
    svc, bridge, tg, _ = world
    code, _ = invite(svc)
    tg.text(1, ANA, f"/start {code}")
    step(bridge, tg)
    assert "até 2 mensagens" in svc.people.admin_command(None, "/limite", "1 2")
    for i, text in enumerate(["/agenda", "/memorias", "/lembretes", "/ajuda"], start=2):
        tg.text(i, ANA, text)
    step(bridge, tg)
    out = said(tg, ANA)
    assert "⏳" in out[-2] and "renova à meia-noite" in out[-2]   # 3ª mensagem barrada
    assert "SEU espaço" in out[-1]                                 # ajuda não conta no limite
    root = svc.people.root / "1"
    assert root.is_dir()
    assert "não tem mais acesso" in svc.people.admin_command(None, "/remover", "1 apagar")
    assert not root.exists()
    tg.text(10, ANA, "/agenda")
    step(bridge, tg)
    assert "privado" in said(tg, ANA)[-1]


def test_admin_can_invite_members_but_not_admins(world):
    svc, bridge, tg, _ = world
    code, _ = invite(svc, "Carla Gerente", "admin")
    tg.text(1, ANA, f"/start {code}")
    tg.text(2, ANA, "/convidar Bruno")
    tg.text(3, ANA, "/convidar Dani admin")
    step(bridge, tg)
    out = said(tg, ANA)
    assert "Convite para Bruno (membro)" in out[-2] and "Só o dono cria administradores" in out[-1]
    assert [p["nome"] for p in svc.people.directory.list()] == ["Carla Gerente", "Bruno"]
    tg.text(4, OWNER, "/pessoas")
    step(bridge, tg)
    assert "1. Carla Gerente — admin — Telegram ✅" in said(tg, OWNER)[-1] and "convite pendente" in said(tg, OWNER)[-1]


# ---------------------------------------------------------------- painel pessoal
class FakeHandler:
    def __init__(self, token="", body=None):
        self.token, self.body, self.out = token, body, None

    def _bearer(self):
        return self.token

    def _read_json(self):
        return self.body

    def _send_json(self, code, payload):
        self.out = (code, payload)

    def _send_html(self, html):
        self.out = (200, html)


def test_personal_panel_page_and_api(world):
    svc, bridge, tg, _ = world
    people = svc.people
    person, _ = people.directory.add("Ana Souza")
    link = people.panel_message(person)
    assert "/u/matrix#" in link
    token = link.split("#")[1].split()[0]
    h = FakeHandler()
    people.route(h, "GET", "/u/matrix", "")
    assert h.out[0] == 200 and "seu espaço" in h.out[1] and token not in h.out[1]
    h = FakeHandler("token-errado-com-mais-de-vinte-letras")
    people.route(h, "GET", "/u/matrix/api/eu", "")
    assert h.out[0] == 401
    h = FakeHandler(token)
    people.route(h, "GET", "/u/matrix/api/eu", "")
    assert h.out == (200, {"nome": "Ana Souza", "papel": "membro", "agente": engine.AGENT_NAME, "uso": 0,
                           "limite": usuarios.default_limit()})
    h = FakeHandler(token)
    people.route(h, "GET", "/u/matrix/api/commands", "")
    names = {c["command"] for c in h.out[1]["commands"]}
    assert {"/tarefa", "/agenda", "/eu"} <= names and not names & {"/email", "/atualizar", "/pessoas", "/saude"}
    h = FakeHandler(token, {"text": "/tarefa Estudar inglês"})
    people.route(h, "POST", "/u/matrix/api/command", "")
    assert h.out[0] == 200 and "Estudar inglês" in h.out[1]["messages"][0]["text"]
    assert people.directory.used(person["n"]) == 1
    assert "Estudar inglês" not in [t["title"] for t in svc.db.list_tasks(status="pending")]
    assert people.status()["pessoas"][0]["painel"] is True


def test_owner_panel_action_and_status(world):
    svc, _, _, _ = world
    res = svc.dispatch_request("people_command", {"cmd": "/convidar", "arg": "Ana"})
    assert "Convite para Ana" in res["text"] and res["pessoas"]["pessoas"][0]["convite"] is True
    assert svc.dispatch_request("status", {})["pessoas"]["pessoas"][0]["nome"] == "Ana"
    assert "/u/" in svc.dispatch_request("people_command", {"cmd": "/pessoas", "arg": "painel 1"})["text"]


def test_spaces_come_back_after_restart(tmp_path, monkeypatch):
    from test_native_tools import TOOL_MODEL, FakeOllama
    fake = FakeOllama()
    svc = headless.build_service(tmp_path, ollama_url=fake.url, model=TOOL_MODEL, profile="pessoal", web=False)
    person, code = svc.people.directory.add("Ana")
    svc.people.directory.redeem(code, ANA)
    svc.people.space(person).service.db.add_task(title="Ler um livro", priority=3)
    svc.shutdown()
    svc2 = headless.build_service(tmp_path, ollama_url=fake.url, model=TOOL_MODEL, profile="pessoal", web=False)
    assert svc2.people.load_all() == 1
    sp = svc2.people.space(svc2.people.directory.by_chat(ANA))
    assert [t["title"] for t in sp.service.db.list_tasks(status="pending")] == ["Ler um livro"]
    svc2.shutdown()
    fake.httpd.shutdown()
    engine.apply_profile("pessoal")


# ---------------------------------------------------------------- nuvem
def test_router_sends_personal_panels_to_the_right_agent():
    router = nuvem.PanelRouter("127.0.0.1", 0, {"t" * 32: 8765}, 8773, {"/u/matrix": 8765, "/u/bit": 8774})
    assert router.target("/u/bit", {}) == (8774, False)
    assert router.target("/u/bit/api/command", {}) == (8774, False)
    assert router.target("/u/bitcoin", {}) == (8773, False)
    assert router.target("/ui?token=" + "t" * 32, {}) == (8765, True)
    router.server.server_close()
    assert nuvem.public_address({"BYTE_ENDERECO": "assistente.empresa.com.br/"}) == "https://assistente.empresa.com.br"


def test_each_guest_has_its_own_queue(tmp_path):
    """Sem o atalho síncrono: a resposta de cada convidado sai da fila dele, sem travar a ponte do dono."""
    from test_native_tools import TOOL_MODEL, FakeOllama
    fake, tg = FakeOllama(), FakeTelegram()
    svc = headless.build_service(tmp_path, ollama_url=fake.url, model=TOOL_MODEL, profile="pessoal", web=False)
    bridge = TelegramBridge(svc, TOKEN, [OWNER], api_base=tg.url, poll_timeout_s=0)
    try:
        for chat, name in ((ANA, "Ana"), (BRUNO, "Bruno")):
            person, code = svc.people.directory.add(name)
            svc.people.directory.redeem(code, chat)
        tg.text(1, ANA, "/tarefa Correr")
        tg.text(2, BRUNO, "/tarefa Nadar")
        bridge.poll_once()
        deadline = time.time() + 10
        while time.time() < deadline and not (said(tg, ANA) and said(tg, BRUNO)):
            time.sleep(0.05)
        assert "Correr" in said(tg, ANA)[-1] and "Nadar" in said(tg, BRUNO)[-1]
    finally:
        svc.shutdown()
        fake.httpd.shutdown()
        tg.httpd.shutdown()
        engine.apply_profile("pessoal")

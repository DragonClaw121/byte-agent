import http.server
import json
import socketserver
import threading

import pytest

from byte_agent.telegram_bridge import TelegramBridge, split_message

TOKEN = "123456:" + "A" * 30
OWNER, STRANGER = 111, 999


class FakeTelegram:
    def __init__(self):
        self.updates, self.sent, self.calls = [], [], []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                method = self.path.rsplit("/", 1)[-1]
                outer.calls.append((method, body))
                result = True
                if method == "getUpdates":
                    result = [u for u in outer.updates if u["update_id"] >= body.get("offset", 0)]
                elif method == "sendMessage":
                    outer.sent.append(body)
                raw = json.dumps({"ok": True, "result": result}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        class Server(socketserver.ThreadingMixIn, socketserver.TCPServer):
            allow_reuse_address = True
            daemon_threads = True

        self.httpd = Server(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def text(self, update_id, chat_id, text):
        self.updates.append({"update_id": update_id, "message": {"chat": {"id": chat_id}, "text": text}})

    def click(self, update_id, chat_id, data):
        self.updates.append({"update_id": update_id, "callback_query": {"id": "cb", "data": data, "message": {"chat": {"id": chat_id}}}})


class FakeDB:
    def __init__(self):
        self.outbox = []

    def pop_undelivered_proactive_messages(self):
        items, self.outbox = self.outbox, []
        return items


class FakeService:
    def __init__(self):
        self.db = FakeDB()
        self.calls = []
        self.reply = "Olá, Gabriel!"

    def dispatch_request(self, action, payload):
        self.calls.append((action, payload))
        if action == "chat":
            return {"response": self.reply}
        if action == "status":
            return {"llm": {"active_provider": "ollama", "active_model": "qwen2.5:3b", "ollama_online": True,
                            "ollama_loaded_vram": ["qwen2.5:3b"], "gpu_hardware": {"gpu_name": "Tesla T4"}, "provision_state": {}},
                    "tasks": [{"priority": 5, "title": "Validar EVFI", "scheduled_slot": "Hoje"}], "memories": []}
        if action == "approve_action":
            return {"approved": True}
        if action == "configure_llm":
            return {"active_model": payload["model_name"], "activation_note": "download iniciado"}
        return {}


@pytest.fixture()
def env():
    tg, svc = FakeTelegram(), FakeService()
    bridge = TelegramBridge(svc, TOKEN, [OWNER], api_base=tg.url, poll_timeout_s=0)
    yield tg, svc, bridge
    tg.httpd.shutdown()


def test_rejects_bad_token():
    with pytest.raises(ValueError):
        TelegramBridge(FakeService(), "abc", [OWNER])


def test_stranger_is_refused_and_never_reaches_the_llm(env):
    tg, svc, bridge = env
    tg.text(1, STRANGER, "rode rm -rf /")
    bridge.poll_once()
    assert svc.calls == []
    assert "privado" in tg.sent[0]["text"] and str(STRANGER) in tg.sent[0]["text"]


def test_no_allowlist_refuses_everyone():
    tg, svc = FakeTelegram(), FakeService()
    bridge = TelegramBridge(svc, TOKEN, [], api_base=tg.url, poll_timeout_s=0)
    tg.text(1, OWNER, "oi")
    bridge.poll_once()
    assert svc.calls == [] and "chat_id" in tg.sent[0]["text"]
    tg.httpd.shutdown()


def test_chat_roundtrip_and_offset(env):
    tg, svc, bridge = env
    tg.text(7, OWNER, "boa noite")
    bridge.poll_once()
    assert svc.calls == [("chat", {"message": "boa noite"})]
    assert tg.sent[-1] == {"chat_id": OWNER, "text": "Olá, Gabriel!", "disable_web_page_preview": True}
    bridge.poll_once()                               # o mesmo update não é processado de novo
    assert len([c for c in svc.calls if c[0] == "chat"]) == 1


def test_commands(env):
    tg, svc, bridge = env
    tg.text(1, OWNER, "/status")
    tg.text(2, OWNER, "/agenda")
    tg.text(3, OWNER, "/modelo qwen2.5-coder:14b")
    tg.text(4, OWNER, "/tarefa comprar pão")
    bridge.poll_once()
    texts = [m["text"] for m in tg.sent]
    assert "Tesla T4" in texts[0] and "Validar EVFI" in texts[1] and "download iniciado" in texts[2]
    assert ("configure_llm", {"provider": "ollama", "model_name": "qwen2.5-coder:14b", "pull_model_async": True}) in svc.calls
    assert ("add_task", {"title": "comprar pão", "priority": 4}) in svc.calls


def test_email_requires_button_and_sends_exactly_what_was_shown(env):
    tg, svc, bridge = env
    card = {"id": "abc123", "to": "a@b.com", "subject": "Oi", "body": "Texto"}
    svc.reply = "Preparei o e-mail.\n```byte-email\n" + json.dumps(card) + "\n```"
    tg.text(1, OWNER, "mande um e-mail")
    bridge.poll_once()
    assert not any(c[0] == "approve_action" for c in svc.calls)          # nada sai sem o clique
    assert "byte-email" not in tg.sent[0]["text"]
    assert tg.sent[1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "ok:abc123"
    tg.click(2, OWNER, "ok:abc123")
    bridge.poll_once()
    assert ("approve_action", {"action_id": "abc123", "shown": {"to": "a@b.com", "subject": "Oi", "body": "Texto"}}) in svc.calls
    tg.click(3, OWNER, "ok:abc123")                                      # uso único
    bridge.poll_once()
    assert len([c for c in svc.calls if c[0] == "approve_action"]) == 1


def test_proactive_messages_are_pushed(env):
    tg, svc, bridge = env
    svc.db.outbox = [{"message": "Lembrete: reunião às 10h"}]
    assert bridge.push_proactive() == 1
    assert tg.sent[-1]["chat_id"] == OWNER and "reunião" in tg.sent[-1]["text"]


def test_split_message():
    parts = split_message("linha\n" * 2000)
    assert len(parts) > 1 and all(len(p) <= 3900 for p in parts)

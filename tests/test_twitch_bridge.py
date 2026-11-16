import http.server
import json
import os
import stat
import threading
import urllib.parse

import pytest

from byte_agent.twitch_bridge import TwitchAuth, TwitchChat, parse_irc_line, sanitize_outgoing
from fakes import FakeTwitchIRC, wait_until


@pytest.fixture()
def irc():
    server = FakeTwitchIRC()
    yield server
    server.close()


def make_chat(irc, auth, inbox):
    chat = TwitchChat("bytebot", auth, "#Canal", on_message=inbox.append, host="127.0.0.1", port=irc.port, use_tls=False)
    chat.start()
    return chat


def test_parse_irc_line_with_tags():
    tags, prefix, command, params = parse_irc_line(
        "@badges=moderator/1;display-name=Fulano\\sSilva;mod=1 :fulano!fulano@fulano.tmi.twitch.tv PRIVMSG #canal :oi : tudo bem?")
    assert tags["display-name"] == "Fulano Silva" and prefix.startswith("fulano!")
    assert command == "PRIVMSG" and params == ["#canal", "oi : tudo bem?"]
    assert parse_irc_line("PING :tmi.twitch.tv")[2:] == ("PING", ["tmi.twitch.tv"])


def test_outgoing_text_cannot_become_a_command_or_raw_irc():
    assert sanitize_outgoing("/ban fulano") == "ban fulano"
    assert sanitize_outgoing(" ./timeout x 600") == "timeout x 600"
    assert sanitize_outgoing("!title hackeado") == "title hackeado"
    assert sanitize_outgoing("oi\r\nPRIVMSG #outro :spam") == "oi PRIVMSG #outro :spam"
    assert len(sanitize_outgoing("a" * 2000)) == 450


def test_login_join_receive_ping_and_send(irc):
    inbox = []
    chat = make_chat(irc, TwitchAuth("oauth:goodtoken"), inbox)
    assert chat.joined.wait(5)
    assert irc.logins == ["goodtoken"] and "NICK bytebot" in irc.lines and "JOIN #canal" in irc.lines
    irc.viewer_says("fulano", "Byte, tudo bem?", mod=True)
    assert wait_until(lambda: len(inbox) == 1)
    msg = inbox[0]
    assert (msg.user, msg.display_name, msg.text, msg.channel, msg.is_mod) == ("fulano", "Fulano", "Byte, tudo bem?", "canal", True)
    irc.push("PING :tmi.twitch.tv")
    assert wait_until(lambda: "PONG :tmi.twitch.tv" in irc.lines)
    assert chat.send("/me olá\r\nQUIT") is True
    assert wait_until(lambda: irc.sent_privmsgs() == ["PRIVMSG #canal :me olá QUIT"])
    chat.stop()


def test_send_rate_limit_and_disconnected(irc):
    chat = make_chat(irc, TwitchAuth("goodtoken"), [])
    assert chat.joined.wait(5)
    results = [chat.send(f"msg {i}") for i in range(25)]
    assert results.count(True) == 18 and results[18:] == [False] * 7       # abaixo do limite de 20 a cada 30 s
    chat.stop()
    assert chat.send("depois de parar") is False


def test_handler_errors_do_not_drop_the_connection(irc):
    def boom(msg):
        raise RuntimeError("falha no tratamento")
    chat = TwitchChat("bytebot", TwitchAuth("goodtoken"), "canal", on_message=boom, host="127.0.0.1", port=irc.port, use_tls=False)
    chat.start()
    assert chat.joined.wait(5)
    irc.viewer_says("a", "x")
    irc.viewer_says("b", "y")
    assert wait_until(lambda: chat.received_count == 2) and chat.joined.is_set()
    chat.stop()


class FakeTokenEndpoint:
    def __init__(self):
        self.forms = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                form = dict(urllib.parse.parse_qsl(self.rfile.read(int(self.headers["Content-Length"])).decode()))
                outer.forms.append(form)
                ok = form.get("refresh_token") == "refresh-1" and form.get("client_secret") == "segredo"
                raw = json.dumps({"access_token": "novotoken", "refresh_token": "refresh-2"} if ok else {"message": "Invalid refresh token"}).encode()
                self.send_response(200 if ok else 400)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/oauth2/token"


def test_expired_token_is_refreshed_and_persisted(tmp_path):
    irc = FakeTwitchIRC(valid_tokens=("novotoken",))
    endpoint = FakeTokenEndpoint()
    store = tmp_path / "twitch_tokens.json"
    auth = TwitchAuth("expirado", "refresh-1", "cid", "segredo", store_path=store, token_url=endpoint.url)
    chat = make_chat(irc, auth, [])
    assert chat.joined.wait(8)
    assert irc.logins == ["expirado", "novotoken"]
    saved = json.loads(store.read_text())
    assert saved == {"client_id": "cid", "access_token": "novotoken", "refresh_token": "refresh-2"}
    assert stat.S_IMODE(os.stat(store).st_mode) == 0o600
    chat.stop()
    # próxima execução: o refresh token do .env já foi gasto; vale o que está guardado
    again = TwitchAuth("expirado", "refresh-1", "cid", "segredo", store_path=store, token_url=endpoint.url)
    assert (again.access_token, again.refresh_token) == ("novotoken", "refresh-2")
    irc.close()
    endpoint.httpd.shutdown()


def test_bad_token_without_refresh_stops_with_a_clear_error(irc):
    chat = make_chat(irc, TwitchAuth("errado"), [])
    assert wait_until(lambda: chat.fatal_error is not None)
    assert "chat:read" in chat.fatal_error and not chat.joined.is_set()
    assert wait_until(lambda: not chat._thread.is_alive())                 # não fica tentando em laço
    assert "errado" not in chat.fatal_error


def test_invalid_names_and_missing_token_are_rejected():
    with pytest.raises(ValueError):
        TwitchChat("x", TwitchAuth("t"), "canal", on_message=print)
    with pytest.raises(ValueError):
        TwitchAuth("")

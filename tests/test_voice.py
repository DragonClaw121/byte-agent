"""v12.3: comando de voz para todos os agentes (Telegram, painel, API e página /voz; Byte só ouve o dono)."""
import argparse
import base64
import http.server
import json
import socketserver
import sys
import threading
import time
import urllib.request

import pytest

from byte_agent import agents, engine, headless, voice
from byte_agent.telegram_bridge import TelegramBridge
from test_native_tools import TOOL_MODEL, FakeOllama
from test_stream import make
from test_telegram_bridge import OWNER, TOKEN, FakeTelegram

API_TOKEN = "v" * 40


class FakeTranscriber:
    available = True

    def __init__(self, text="Kronus, que horas são?"):
        self.text, self.calls = text, []

    def transcribe(self, audio, mime="audio/ogg"):
        self.calls.append((audio, mime))
        return self.text

    def describe(self):
        return "fake"


class FakeSynth:
    available = True

    def __init__(self):
        self.calls = []

    def synthesize(self, text, voice=None, speed=None):
        self.calls.append((text, voice, speed))
        return b"OggS-fake", "audio/ogg"

    def describe(self):
        return "fake"


class FakeOpenAIAudio:
    """Servidor compatível com /audio/transcriptions e /audio/speech (Groq, OpenAI, Kokoro-FastAPI...)."""

    def __init__(self):
        self.requests = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                outer.requests.append((self.path, dict(self.headers), body))
                if self.path.endswith("/audio/transcriptions"):
                    raw = json.dumps({"text": "  olá   mundo "}).encode()
                    ctype = "application/json"
                else:
                    fmt = json.loads(body)["response_format"]
                    if fmt == "opus":
                        self.send_response(422)
                        self.end_headers()
                        return
                    raw, ctype = b"ID3-mp3", "audio/mpeg"
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        class Server(socketserver.ThreadingMixIn, socketserver.TCPServer):
            daemon_threads = True

        self.httpd = Server(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/v1"


def chat_system(fake):
    """Prompt de sistema da última chamada de CHAT (as extrações em segundo plano também passam pelo Ollama)."""
    for body in reversed(fake.requests):
        for m in body.get("messages", []):
            if m["role"] == "system" and "IDENTIDADE" in m["content"]:
                return m["content"]
    raise AssertionError("nenhuma chamada de chat registrada")


@pytest.fixture()
def fake():
    server = FakeOllama()
    yield server
    server.httpd.shutdown()
    engine.apply_profile("pessoal")


def runtime(tmp_path, fake, monkeypatch, agent="kronus", menu=False):
    monkeypatch.setenv("BYTE_WEB", "0")
    monkeypatch.setenv("KRONUS_API_TOKEN", API_TOKEN)
    monkeypatch.chdir(tmp_path)
    args = argparse.Namespace(agent=agent, agent_menu=menu, data_dir=str(tmp_path / "dados"), profile="pessoal",
                              ollama_url=fake.url, model=TOOL_MODEL, host="127.0.0.1", port=0, ui_token="",
                              telegram_token="", telegram_chat_ids="", no_autonomy=True)
    rt = headless.Runtime(args, env={"KRONUS_API_TOKEN": API_TOKEN}).start()
    rt.service.voice_kit = voice.VoiceKit(FakeTranscriber(), FakeSynth())
    return rt


# ------------------------------------------------------------------ regras
def test_wake_words_and_manifest_rules():
    assert voice.match_wake_word("Ei Kronus, que horas são?", ["kronus"]) == (True, "que horas são?")
    assert voice.match_wake_word("Cronos qual a previsão", ["kronus", "cronos"]) == (True, "qual a previsão")
    assert voice.match_wake_word("Byte!", ["byte"]) == (True, "")
    assert voice.match_wake_word("galera, hoje a gente vai jogar com a Byte", ["byte"])[0] is False   # citou, não chamou
    byte, kronus, bit = (agents.resolve_agent(a) for a in ("byte", "kronus", "bit"))
    assert byte["voz"]["somente_dono"] and byte["voz"]["na_live"]
    assert kronus["voz"]["responder_com_voz"] == "sempre" and "cronos" in kronus["voz"]["palavras_chave"]
    assert not bit["voz"]["somente_dono"] and voice.policy_for(None)["nome"] == "Matrix"
    with pytest.raises(agents.ManifestError, match="na_live"):
        agents.validate_manifest({"id": "xx", "tipo": "assistente", "voz": {"na_live": True}})
    with pytest.raises(agents.ManifestError, match="responder_com_voz"):
        agents.validate_manifest({"id": "xx", "voz": {"responder_com_voz": "às vezes"}})
    assert agents.validate_manifest({"id": "zeca", "nome": "Zeca"})["voz"]["palavras_chave"] == ["zeca"]


def test_openai_compatible_transcription_and_speech():
    srv = FakeOpenAIAudio()
    try:
        stt = voice.Transcriber(url=srv.url, model="whisper-large-v3-turbo", api_key="chave-de-teste")
        assert stt.available and stt.transcribe(b"OggS...", "audio/ogg") == "olá mundo"
        path, headers, body = srv.requests[0]
        assert path == "/v1/audio/transcriptions" and headers["Authorization"] == "Bearer chave-de-teste"
        assert b'name="model"\r\n\r\nwhisper-large-v3-turbo' in body and b'filename="voz.ogg"' in body and b"OggS..." in body
        tts = voice.Synthesizer(url=srv.url, voice="pf_dora")
        assert tts.synthesize("Oi! **Tudo** certo? https://x.com 😀") == (b"ID3-mp3", "audio/mpeg")   # sem opus: cai para mp3
        sent = json.loads(srv.requests[-1][2])
        assert sent["input"] == "Oi! Tudo certo?" and sent["voice"] == "pf_dora"
    finally:
        srv.httpd.shutdown()
    with pytest.raises(voice.VoiceError, match="não configurada"):
        voice.Transcriber(backend="").transcribe(b"x")


# ------------------------------------------------------------------ comando de voz
def test_kronus_voice_is_short_and_spoken(tmp_path, fake, monkeypatch):
    rt = runtime(tmp_path, fake, monkeypatch)
    try:
        res = rt.service.dispatch_request("voice_command", {"audio_b64": base64.b64encode(b"OggS").decode(), "mime": "audio/ogg",
                                                            "source": "api", "owner": True})
        assert res["heard"] == "Kronus, que horas são?" and res["response"] and res["audio_mime"] == "audio/ogg"
        system = chat_system(fake)
        assert "no máximo 2 frases curtas" in system and "seu nome é Kronus" in system
        hands = rt.service.dispatch_request("voice_command", {"text": "estava pensando aqui", "hands_free": True, "owner": True})
        assert hands["ignored"] and "response" not in hands
        assert rt.service.dispatch_request("voice_command", {"text": "Kronus", "hands_free": True, "owner": True})["wake_only"]
        st = rt.service.dispatch_request("status", {})["voice"]
        assert st["enabled"] and st["stt"] == "fake" and "kronus" in st["wake_words"]
    finally:
        rt.stop()


def test_byte_obeys_only_the_owner_and_answers_in_the_live(tmp_path, fake, monkeypatch):
    rt = runtime(tmp_path, fake, monkeypatch, agent="byte")
    try:
        denied = rt.service.dispatch_request("voice_command", {"text": "Byte, diz oi pro chat", "owner": False})
        assert "só aceita comando de voz do dono" in denied["error"]
        director, llm, chat, avatar, speech = make(["[feliz] Oi, Gabriel! Bora jogar!"])
        rt.service.live_owner_sink = director.owner_says
        res = rt.service.dispatch_request("voice_command", {"text": "Byte, diz oi pro chat", "owner": True, "hands_free": True})
        assert res["live"] and res["queued"]
        director.set_paused(False)
        director.paused = True                      # live pausada para o chat: a voz do dono ainda é atendida
        out = director.step()
        assert out["text"] == "Oi, Gabriel! Bora jogar!" and speech.spoken == ["Oi, Gabriel! Bora jogar!"] and chat.sent == []
        prompt = llm.calls[-1]["messages"][-1]["content"]
        assert "POR VOZ" in prompt and "seu dono" in prompt and "diz oi pro chat" in prompt
        assert director.stats["owner_voice"] == 1
    finally:
        rt.stop()


def test_voice_api_and_page(tmp_path, fake, monkeypatch):
    rt = runtime(tmp_path, fake, monkeypatch)
    base = f"http://127.0.0.1:{rt.port}"
    try:
        page = urllib.request.urlopen(base + "/voz", timeout=5).read().decode()
        assert "Mãos livres" in page and "/api/v1/voice" in page and API_TOKEN not in page

        def post(body, ctype, token=API_TOKEN):
            req = urllib.request.Request(base + "/api/v1/voice", data=body, method="POST",
                                         headers={"Content-Type": ctype, "Authorization": f"Bearer {token}"})
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    return resp.status, json.loads(resp.read())
            except urllib.error.HTTPError as err:
                return err.code, json.loads(err.read() or b"{}")

        assert post(b'{"text": "oi"}', "application/json", token="errado")[0] == 401
        code, res = post(json.dumps({"text": "Kronus, me lembra de beber água"}).encode(), "application/json")
        assert code == 200 and res["agent"] == "Kronus" and res["reply"] and res["audio_b64"]
        code, res = post(b"\x1aE\xdf\xa3webm", "audio/webm")
        assert code == 200 and res["heard"] == "Kronus, que horas são?"
        assert rt.service.voice_kit.transcriber.calls[-1] == (b"\x1aE\xdf\xa3webm", "audio/webm")
        status = json.loads(urllib.request.urlopen(urllib.request.Request(
            base + "/api/v1/status", headers={"Authorization": f"Bearer {API_TOKEN}"}), timeout=5).read())
        assert status["voice"]["reply_with_voice"] == "sempre"
    finally:
        rt.stop()


def test_telegram_voice_notes(tmp_path, fake, monkeypatch):
    tg = FakeTelegram()
    rt = runtime(tmp_path, fake, monkeypatch, agent="byte")
    try:
        bridge = TelegramBridge(rt.service, TOKEN, [OWNER, -100200], api_base=tg.url, poll_timeout_s=0)
        sys.__dict__.setdefault("_byte_telegram_bridges", {})["teste"] = bridge
        rt.bridge = bridge
        rt.service.voice_kit.transcriber.text = "Byte, como foi a live?"
        tg.files["voz1"] = b"OggS-nota"
        tg.updates.append({"update_id": 1, "message": {"chat": {"id": OWNER, "type": "private"},
                                                       "voice": {"file_id": "voz1", "mime_type": "audio/ogg", "file_size": 9}}})
        tg.updates.append({"update_id": 2, "message": {"chat": {"id": -100200, "type": "group"},
                                                       "voice": {"file_id": "voz1", "mime_type": "audio/ogg", "file_size": 9}}})
        bridge.poll_once()
        texts = [m["text"] for m in tg.sent]
        assert texts[0] == "🎙️ Ouvi: “Byte, como foi a live?”" and texts[1]           # resposta em texto
        assert ("sendVoice", {}) in tg.calls                                             # e em áudio (nota de voz)
        assert "só aceita comando de voz do dono" in texts[-1]                          # áudio do grupo: ignorado
        assert rt.service.voice_kit.transcriber.calls[0] == (b"OggS-nota", "audio/ogg")
    finally:
        sys.__dict__.get("_byte_telegram_bridges", {}).pop("teste", None)
        rt.stop()
        tg.httpd.shutdown()


def test_panel_voice_flag_and_identity(tmp_path, fake, monkeypatch):
    rt = runtime(tmp_path, fake, monkeypatch, agent="bit")
    try:
        res = rt.service.dispatch_request("chat", {"message": "Bit, como estão as vendas?", "voice": True})
        assert res["heard"] and res["speech"] and "audio_b64" not in res               # painel fala com a voz do navegador
        system = chat_system(fake)
        assert "comece pela conclusão" in system
        html = engine.build_v90_web_ui(rt.service.dispatch_request("status", {}))
        assert 'id="b90-mic-btn"' in html and 'id="b90-voice-hands"' in html
    finally:
        rt.stop()
    engine.apply_profile("pessoal")
    guard = engine.identity_guard()
    assert "seu nome é Matrix" in guard and "Byte v10.1" in guard
    assert "Byte" not in engine.ByteEcosystemService.PROACTIVE_TURN_MARKER if hasattr(engine.ByteEcosystemService, "PROACTIVE_TURN_MARKER") else True


def test_root_from_outside_redirects_to_voice_page(tmp_path, fake, monkeypatch):
    rt = runtime(tmp_path, fake, monkeypatch)
    try:
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", rt.port, timeout=5)
        conn.request("GET", "/", headers={"Host": "notebook.tail1234.ts.net"})
        resp = conn.getresponse()
        assert resp.status == 302 and resp.getheader("Location") == "/voz"
        conn.request("GET", f"/ui?token={rt.server.token}", headers={"Host": "notebook.tail1234.ts.net"})
        resp2 = conn.getresponse()
        assert resp2.status == 302 and b"Painel" not in resp2.read()          # o painel nunca abre de fora
    finally:
        rt.stop()


def test_each_agent_has_its_own_voice(tmp_path, fake, monkeypatch):
    """v12.4: voz do servidor, tom e velocidade por agente; voz desconhecida no serviço cai para a padrão."""
    seen = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            seen.append(body)
            if body["voice"] not in {"pf_dora", "pm_alex"}:
                self.send_response(400)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Length", "4")
            self.end_headers()
            self.wfile.write(b"OggS")

    srv = socketserver.TCPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        tts = voice.Synthesizer(url=f"http://127.0.0.1:{srv.server_address[1]}/v1", voice="pf_dora")
        assert tts.synthesize("Oi", voice="pm_alex", speed=0.95) == (b"OggS", "audio/ogg")
        assert seen[-1]["voice"] == "pm_alex" and seen[-1]["speed"] == 0.95
        assert tts.synthesize("Oi", voice="voz_que_nao_existe", speed=1.0)[0] == b"OggS"     # recusada: usa a padrão
        assert seen[-1]["voice"] == "pf_dora" and "speed" not in seen[-1] and "recusada" in tts.last_voice_error

        rt = runtime(tmp_path, fake, monkeypatch, agent="bit")
        try:
            rt.service.voice_kit = voice.VoiceKit(FakeTranscriber("Bit, resumo das vendas"), tts)
            res = rt.service.dispatch_request("voice_command", {"audio_b64": base64.b64encode(b"x").decode(), "owner": True})
            assert res["audio_b64"] and seen[-1]["voice"] == "pm_alex"
            st = rt.service.dispatch_request("status", {})["voice"]
            assert st["server_voice"] == "pm_alex" and st["pitch"] == 0.85 and st["rate"] == 0.95
        finally:
            rt.stop()
    finally:
        srv.shutdown()
    with pytest.raises(agents.ManifestError, match="tom"):
        agents.validate_manifest({"id": "xx", "voz": {"tom": 3}})
    with pytest.raises(agents.ManifestError, match="voz_servidor"):
        agents.validate_manifest({"id": "xx", "voz": {"voz_servidor": "pf dora; rm -rf"}})
    assert voice.policy_for(None)["velocidade"] == 0.95


def test_transcription_passes_cloudflare_signature_check():
    """v12.7: Groq/OpenAI ficam atrás do Cloudflare, que barra 'Python-urllib' com 403 / error code: 1010."""
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            if self.headers.get("User-Agent", "").startswith("Python-urllib"):
                self.send_response(403)
                self.send_header("Content-Length", "16")
                self.end_headers()
                self.wfile.write(b"error code: 1010")
                return
            raw = json.dumps({"text": "funcionou"}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    srv = socketserver.TCPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}/openai/v1"
        assert voice.Transcriber(url=url, api_key="k").transcribe(b"OggS") == "funcionou"
        req = urllib.request.Request(url + "/audio/transcriptions", data=b"x", method="POST")      # o padrão do Python é barrado
        with pytest.raises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(req, timeout=5)
        assert err.value.code == 403
    finally:
        srv.shutdown()


def test_transcription_retries_flaky_network_but_not_bad_keys(monkeypatch):
    """v12.9: timeouts e 5xx/429 são repetidos (até 3 vezes); chave errada (401) falha na hora."""
    monkeypatch.setattr(voice, "_retry_pause", lambda s: None)
    plan = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            step = plan.pop(0)
            if step == "lento":
                time.sleep(1.5)                       # passa do limite de 1 s da tentativa
                step = 200
            if step != 200:
                self.send_response(step)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")
                return
            raw = json.dumps({"text": "chegou"}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    class Server(socketserver.ThreadingMixIn, socketserver.TCPServer):
        daemon_threads = True

    srv = Server(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        stt = voice.Transcriber(url=f"http://127.0.0.1:{srv.server_address[1]}/v1", timeout_s=1.0)
        plan[:] = ["lento", 503, 200]
        assert stt.transcribe(b"OggS") == "chegou" and plan == []
        plan[:] = [429, 429, 429]
        with pytest.raises(voice.VoiceError, match="3 tentativas"):
            stt.transcribe(b"OggS")
        plan[:] = [401, 200]
        with pytest.raises(voice.VoiceError, match="HTTP 401"):
            stt.transcribe(b"OggS")
        assert plan == [200]                          # não repetiu
    finally:
        srv.shutdown()


def test_telegram_says_what_it_heard_before_the_slow_reply(tmp_path, fake, monkeypatch):
    tg = FakeTelegram()
    rt = runtime(tmp_path, fake, monkeypatch, agent="kronus")
    try:
        bridge = TelegramBridge(rt.service, TOKEN, [OWNER], api_base=tg.url, poll_timeout_s=0)
        rt.service.voice_kit.transcriber.text = "Kronus, que dia é hoje?"
        tg.files["v1"] = b"OggS"
        seen_at_reply = []
        real_interact = rt.service.interact

        def slow_interact(*a, **kw):                   # o modelo no processador: lento
            seen_at_reply.append([m["text"] for m in tg.sent])
            time.sleep(1.0)
            return real_interact(*a, **kw)

        rt.service.interact = slow_interact
        tg.updates.append({"update_id": 1, "message": {"chat": {"id": OWNER, "type": "private"},
                                                       "voice": {"file_id": "v1", "mime_type": "audio/ogg", "file_size": 4}}})
        bridge.poll_once()
        assert seen_at_reply == [["🎙️ Ouvi: “Kronus, que dia é hoje?”"]]       # o "Ouvi" saiu antes de o modelo responder
        assert ("sendVoice", {}) in tg.calls                                     # Kronus responde em áudio (sempre)
        assert sum(1 for c in tg.calls if c[0] == "sendChatAction") >= 1
    finally:
        rt.stop()
        tg.httpd.shutdown()

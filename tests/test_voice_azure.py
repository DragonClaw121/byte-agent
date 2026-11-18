"""v13.7: fala das respostas pelo Azure AI Speech (plano gratuito, vozes pt-BR)."""
import http.server
import threading

import pytest

from byte_agent import voice


class FakeSpeech(http.server.BaseHTTPRequestHandler):
    calls = []
    reject_styles = False
    known = {"pt-BR-FranciscaNeural", "pt-BR-ThalitaNeural", "pt-BR-AntonioNeural", "pt-BR-ValerioNeural"}

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()
        FakeSpeech.calls.append({"key": self.headers.get("Ocp-Apim-Subscription-Key"), "fmt": self.headers.get("X-Microsoft-OutputFormat"),
                                 "ctype": self.headers.get("Content-Type"), "ssml": body})
        if self.headers.get("Ocp-Apim-Subscription-Key") != "chave-certa":
            self.send_response(401); self.send_header("Content-Length", "0"); self.end_headers(); return
        if not any(f'name="{v}"' in body for v in self.known) or (FakeSpeech.reject_styles and "express-as" in body):
            self.send_response(400); self.send_header("Content-Length", "0"); self.end_headers(); return
        data = b"OggS-audio"
        self.send_response(200)
        self.send_header("Content-Type", "audio/ogg")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture()
def speech_url():
    FakeSpeech.calls = []
    FakeSpeech.reject_styles = False
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeSpeech)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}/cognitiveservices/v1"
    srv.shutdown()


def test_azure_speech_is_picked_from_env_and_builds_the_regional_endpoint():
    s = voice.Synthesizer.from_env({"AZURE_SPEECH_KEY": "k", "AZURE_SPEECH_REGION": "Brazil South"})
    assert s.available and s.backend == "azure" and s.describe() == "Azure AI Speech"
    assert s.azure_endpoint == "https://brazilsouth.tts.speech.microsoft.com/cognitiveservices/v1"
    assert voice.Synthesizer.from_env({"AZURE_SPEECH_KEY": "k"}).available is False        # sem região, nada
    assert voice.Synthesizer.from_env({"AZURE_SPEECH_KEY": "k", "AZURE_SPEECH_REGION": "x",
                                       "BYTE_VOICE_TTS_URL": "http://127.0.0.1:8880/v1"}).backend == "http"


def test_each_agent_gets_its_own_portuguese_voice_with_speed_and_escaped_text(speech_url):
    s = voice.Synthesizer(azure_key="chave-certa", azure_endpoint=speech_url)
    data, mime = s.synthesize("Oi <Gabriel> & cia", voice="pm_santa", speed=1.1)    # voz do Kronus (Kokoro)
    assert data == b"OggS-audio" and mime == "audio/ogg"
    call = FakeSpeech.calls[-1]
    assert call["fmt"] == "ogg-24khz-16bit-mono-opus" and call["ctype"] == "application/ssml+xml"
    assert 'name="pt-BR-ValerioNeural"' in call["ssml"] and 'xml:lang="pt-BR"' in call["ssml"]
    assert 'rate="+10%"' in call["ssml"] and "Gabriel &amp; cia" in call["ssml"] and "<Gabriel" not in call["ssml"]
    s.synthesize("oi", voice="pt-BR-AntonioNeural")                                  # nome do Azure direto
    assert 'name="pt-BR-AntonioNeural"' in FakeSpeech.calls[-1]["ssml"] and "prosody" not in FakeSpeech.calls[-1]["ssml"]
    s.synthesize("oi")                                                                  # Matrix: voz padrão
    assert 'name="pt-BR-FranciscaNeural"' in FakeSpeech.calls[-1]["ssml"]


def test_unknown_voice_falls_back_to_the_default_but_a_bad_key_is_reported(speech_url):
    s = voice.Synthesizer(azure_key="chave-certa", azure_endpoint=speech_url, voice="pt-BR-FranciscaNeural")
    data, _ = s.synthesize("oi", voice="pt-BR-NaoExisteNeural")
    assert data and "recusada" in (s.last_voice_error or "")
    assert 'name="pt-BR-FranciscaNeural"' in FakeSpeech.calls[-1]["ssml"]
    bad = voice.Synthesizer(azure_key="errada", azure_endpoint=speech_url)
    with pytest.raises(voice.VoiceError, match="HTTP 401"):
        bad.synthesize("oi", voice="pf_dora")


def test_voice_can_be_changed_per_agent_by_environment(monkeypatch):
    monkeypatch.setenv("BYTE_VOZ_KRONUS", "pt-BR-DonatoNeural")
    monkeypatch.setenv("BYTE_VOZ_MATRIX", "pt-BR-LeilaNeural")
    monkeypatch.setenv("BYTE_VOZ_BIT", "voz com espaço;rm")                       # valor estranho é ignorado
    kronus = {"tipo": "assistente", "nome": "Kronus", "voz": dict(voice.VOICE_DEFAULTS["assistente"], voz_servidor="pm_santa")}
    assert voice.policy_for(kronus)["voz_servidor"] == "pt-BR-DonatoNeural"
    assert voice.policy_for(None)["voz_servidor"] == "pt-BR-LeilaNeural"
    bit = {"tipo": "trabalho", "nome": "Bit", "voz": dict(voice.VOICE_DEFAULTS["trabalho"], voz_servidor="pm_alex")}
    assert voice.policy_for(bit)["voz_servidor"] == "pm_alex"


def test_panel_and_voice_page_can_ask_for_the_agents_server_voice(tmp_path, monkeypatch):
    """v13.8: painel e /voz falam com a mesma voz do servidor (Azure) que o Telegram, na voz do agente ativo."""
    import json
    import urllib.error
    import urllib.request
    import test_voice as tv
    fake = tv.FakeOllama()
    monkeypatch.setenv("BYTE_VOZ_KRONUS", "pt-BR-DonatoNeural")
    rt = tv.runtime(tmp_path, fake, monkeypatch)
    base = f"http://127.0.0.1:{rt.port}"
    try:
        synth = rt.service.voice_kit.synthesizer
        res = rt.service.dispatch_request("voice_speak", {"text": "**Oi!** Veja https://exemplo.com agora."})
        assert res["audio_b64"] and res["audio_mime"] == "audio/ogg"
        text, used_voice, speed = synth.calls[-1]
        assert used_voice == "pt-BR-DonatoNeural" and "https" not in text and "*" not in text and speed == 1.1

        def post(body, token=tv.API_TOKEN):
            req = urllib.request.Request(base + "/api/v1/speak", data=body, method="POST",
                                         headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    return resp.status, json.loads(resp.read())
            except urllib.error.HTTPError as err:
                return err.code, json.loads(err.read() or b"{}")

        assert post(b'{"text": "oi"}', token="errado")[0] == 401
        code, out = post(json.dumps({"text": "Bom dia, Gabriel."}).encode())
        assert code == 200 and out["audio_b64"]
        assert post(b"nao-e-json")[0] == 400
        assert post(b'{"text": "   "}')[0] == 503                       # nada para falar
        rt.service.voice_kit.synthesizer = voice.Synthesizer()               # sem voz do servidor
        code, out = post(b'{"text": "oi"}')
        assert code == 503 and "não configurada" in out["error"]
    finally:
        rt.stop()
        fake.httpd.shutdown()


def test_byte_voice_skips_avatar_tags_and_reads_english_terms_right(speech_url, monkeypatch):
    assert voice.speech_text("[feliz] Bora pra live! [animada] 🎮") == "Bora pra live!"
    monkeypatch.setenv("BYTE_VOZ_PRONUNCIAS", "Kronus=crônus; ; =x;palavra-sem-fala=")
    s = voice.Synthesizer(azure_key="chave-certa", azure_endpoint=speech_url)
    s.synthesize("A Byte vai fazer live de games com o Kronus. Livestock não muda.", voice="pt-BR-ThalitaNeural")
    ssml = FakeSpeech.calls[-1]["ssml"]
    assert '<sub alias="báite">Byte</sub>' in ssml and '<sub alias="laive">live</sub>' in ssml
    assert '<sub alias="gueimes">games</sub>' in ssml and '<sub alias="crônus">Kronus</sub>' in ssml
    assert "Livestock não muda" in ssml                                   # palavra parecida não é tocada


def test_speed_per_agent_and_a_brisker_matrix(monkeypatch):
    assert voice.policy_for(None)["velocidade"] == 1.1
    monkeypatch.setenv("BYTE_VOZ_MATRIX_VELOCIDADE", "1,25")
    monkeypatch.setenv("BYTE_VOZ_BYTE_VELOCIDADE", "9")                  # limitado a 2.0
    assert voice.policy_for(None)["velocidade"] == 1.25
    byte = {"tipo": "vtuber", "nome": "Byte", "voz": dict(voice.VOICE_DEFAULTS["vtuber"], velocidade=1.1)}
    assert voice.policy_for(byte)["velocidade"] == 2.0


def test_work_profile_panel_keeps_the_speech_filter_intact():
    """O visual de trabalho remove emojis do painel; o filtro da fala não pode depender de um emoji literal
    (antes virava /[^\\n]*/g, apagava o texto inteiro e o Bit ficava mudo)."""
    from byte_agent import engine
    engine.apply_profile("trabalho", {"usuario": "X"})
    try:
        html = engine.build_v90_web_ui({}, token="t" * 43)
    finally:
        engine.apply_profile("pessoal")
    assert "replace(/[^\\n]*/g" not in html and "\\u{1F6E1}" in html


def test_foreign_multilingual_voice_is_told_to_speak_brazilian_portuguese(speech_url):
    FakeSpeech.known = FakeSpeech.known | {"zh-CN-XiaoxiaoMultilingualNeural"}
    s = voice.Synthesizer(azure_key="chave-certa", azure_endpoint=speech_url)
    s.synthesize("Oi chat, bora pra live?", voice="zh-CN-XiaoxiaoMultilingualNeural", speed=1.1)
    ssml = FakeSpeech.calls[-1]["ssml"]
    assert 'name="zh-CN-XiaoxiaoMultilingualNeural"' in ssml and 'xml:lang="zh-CN"' in ssml
    assert '<lang xml:lang="pt-BR"><prosody rate="+10%">' in ssml and '<sub alias="laive">live</sub>' in ssml
    s.synthesize("Oi", voice="pt-BR-AntonioNeural")                       # voz brasileira: sem <lang>
    assert "<lang" not in FakeSpeech.calls[-1]["ssml"]


def test_byte_speaks_cheerful_by_default_and_style_is_configurable(speech_url, monkeypatch):
    byte = {"tipo": "vtuber", "nome": "Byte", "voz": voice.validate_voice({"voz_servidor": "pf_dora"}, "vtuber", "Byte")}
    pol = voice.policy_for(byte)
    assert pol["estilo"] == "cheerful" and pol["intensidade"] == 1.3
    assert voice.policy_for(None)["estilo"] == ""                          # Matrix: sem estilo
    s = voice.Synthesizer(azure_key="chave-certa", azure_endpoint=speech_url)
    s.synthesize("Oi chat!", voice="zh-CN-XiaoxiaoMultilingualNeural", speed=1.0, style=pol["estilo"], style_degree=pol["intensidade"])
    ssml = FakeSpeech.calls[-1]["ssml"]
    assert '<mstts:express-as style="cheerful" styledegree="1.30"><lang xml:lang="pt-BR">' in ssml
    monkeypatch.setenv("BYTE_VOZ_BYTE_ESTILO", "affectionate")
    monkeypatch.setenv("BYTE_VOZ_BYTE_INTENSIDADE", "1,6")
    assert (voice.policy_for(byte)["estilo"], voice.policy_for(byte)["intensidade"]) == ("affectionate", 1.6)
    monkeypatch.setenv("BYTE_VOZ_BYTE_ESTILO", "")                        # vazio desliga o estilo
    assert voice.policy_for(byte)["estilo"] == ""
    with pytest.raises(ValueError, match="estilo"):
        voice.validate_voice({"estilo": "<script>"}, "vtuber", "Byte")
    with pytest.raises(ValueError, match="intensidade"):
        voice.validate_voice({"intensidade": 5}, "vtuber", "Byte")


def test_style_refused_keeps_the_same_voice_without_style(speech_url):
    FakeSpeech.known = FakeSpeech.known | {"zh-CN-XiaoxiaoMultilingualNeural"}
    FakeSpeech.reject_styles = True
    s = voice.Synthesizer(azure_key="chave-certa", azure_endpoint=speech_url)
    data, _ = s.synthesize("Oi", voice="zh-CN-XiaoxiaoMultilingualNeural", style="cheerful", style_degree=1.3)
    assert data and "estilo 'cheerful' recusado" in s.last_voice_error
    last = FakeSpeech.calls[-1]["ssml"]
    assert 'name="zh-CN-XiaoxiaoMultilingualNeural"' in last and "express-as" not in last   # mesma voz, sem estilo

"""v13.36 (R2.1): reuniões com transcrição ao vivo (pedaços do app) e ata no fim."""
import io
import json
import subprocess
import wave

import docx
import pytest

from byte_agent import engine, headless, reunioes


def wav(seconds=1.0):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * int(16000 * seconds))
    return buf.getvalue()


FALAS = (["Bom dia, vamos começar. A Ana apresenta o orçamento.",
              "Decidimos aprovar o orçamento de 50 mil. Ana manda a proposta até sexta.",
              "O João vai revisar o contrato na segunda."])
ATA = {"resumo": "Reunião de orçamento. O orçamento de 50 mil foi aprovado.", "participantes": ["Ana", "João"],
       "decisoes": ["Aprovar o orçamento de 50 mil"],
       "tarefas": [{"o_que": "Enviar a proposta", "quem": "Ana", "prazo": "sexta"},
                   {"o_que": "Revisar o contrato", "quem": "João", "prazo": "segunda"}],
       "pontos_em_aberto": [], "proximos_passos": ["Assinar o contrato"]}


@pytest.fixture()
def svc(tmp_path):
    from test_native_tools import TOOL_MODEL, FakeOllama
    fake = FakeOllama()
    service = headless.build_service(tmp_path, ollama_url=fake.url, model=TOOL_MODEL, profile="pessoal", web=False)
    seen = {"prompts": [], "llm": []}
    falas = iter(FALAS)

    def transcribe(audio, mime, prompt):
        seen["prompts"].append(prompt)
        return next(falas, "fala extra")

    def llm(messages, max_tokens):
        seen["llm"].append(messages)
        return json.dumps(ATA)
    service.meetings._transcribe, service.meetings._llm = transcribe, llm
    service.seen = seen
    yield service
    service.proactive_engine.stop()
    fake.httpd.shutdown()
    engine.apply_profile("pessoal")


def test_live_meeting_minutes_and_tasks_card(svc):
    m = svc.meetings
    mid = m.start("Orçamento 2027")["id"]
    first = m.add_chunk(mid, wav(20), seq=0, offset_s=0)
    assert first["texto"].startswith("[00:00] Bom dia")
    assert m.add_chunk(mid, wav(20), seq=0)["repetido"]                         # o app reenviou: ignorado
    m.add_chunk(mid, wav(22), seq=1, offset_s=20)
    m.add_chunk(mid, wav(10), seq=2, offset_s=42)
    assert "Bom dia" in svc.seen["prompts"][1]                                   # contexto para o Whisper
    got = m.get(mid)
    assert got["transcript"].count("\n") == 3 and "[00:42] O João" in got["transcript"] and got["seconds"] == 52
    m.finish(mid, background=False)
    with pytest.raises(ValueError, match="encerrada"):
        m.add_chunk(mid, wav(), seq=5)
    got = m.get(mid)
    assert got["status"] == "pronta" and "📌 Tarefas:\n• Enviar a proposta — Ana (até sexta)" in got["ata_text"]
    system = svc.seen["llm"][-1][0]["content"]
    assert "CONTEÚDO DE TERCEIROS" in system and "<transcricao>" in svc.seen["llm"][-1][1]["content"]
    d = docx.Document(str(m.outputs / got["file"]))
    assert d.tables[0].rows[1].cells[0].text == "Enviar a proposta"
    msgs = [x["message"] for x in svc.db.pop_undelivered_proactive_messages("telegram")]
    ata_msg = next(x for x in msgs if x.startswith("📝 Ata: Orçamento 2027"))
    assert "Arquivos gerados em entregas/: ata_" in ata_msg and "byte-action" in ata_msg
    card = json.loads(ata_msg.split("```byte-action\n")[1].split("\n```")[0])
    assert card["title"] == "Criar 2 tarefa(s) da reunião \"Orçamento 2027\""
    svc.approve_pending_action(card["id"], {"title": card["title"], "details": card["details"]})
    titles = [t["title"] for t in svc.db.list_tasks(status="pending")]
    assert "Enviar a proposta — Ana" in titles and "Revisar o contrato — João" in titles


def test_long_transcript_is_summarized_in_parts(svc):
    m = svc.meetings
    mid = m.start("Longa")["id"]
    with svc.db.connection() as conn:
        conn.execute("UPDATE meetings SET transcript = ? WHERE id = ?", ("[00:00] fala " * 3000, mid))
    m._llm = lambda messages, n: json.dumps({"topicos": ["ponto A"]}) if "parte" in messages[0]["content"] else json.dumps(ATA)
    m.finish(mid, background=False)
    assert m.get(mid)["status"] == "pronta"


def test_recorded_file_is_split_with_ffmpeg(svc, tmp_path):
    exe = reunioes.ffmpeg_path()
    if not exe:
        pytest.skip("sem ffmpeg")
    video = tmp_path / "reuniao.mp4"
    subprocess.run([exe, "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=5", "-f", "lavfi",
                    "-i", "color=c=black:s=64x64:d=5", "-shortest", str(video)], check=True)
    parts = reunioes.split_media(video.read_bytes(), ".mp4", segment_s=2)
    assert len(parts) == 3 and all(p[:3] == b"ID3" or p[:2] == b"\xff\xfb" or len(p) > 100 for p in parts)
    got = svc.meetings.from_file(video.read_bytes(), "reuniao.mp4", "video/mp4", "Gravação do Teams", background=False)
    assert svc.meetings.get(got["id"])["status"] == "pronta"


def test_api_route(svc):
    class H:
        def __init__(self, body=b"", ctype="application/json"):
            import io as _io
            self.rfile, self.headers, self.sent = _io.BytesIO(body), {"Content-Length": str(len(body)), "Content-Type": ctype}, None

        def _read_json(self):
            return json.loads(self.rfile.read(int(self.headers["Content-Length"])) or b"{}")

        def _send_json(self, code, data):
            self.sent = (code, data)
    route = svc.api_routes["/api/v1/reuniao"]
    h = H(json.dumps({"titulo": "Daily"}).encode())
    route(h, "POST", "/api/v1/reuniao", "")
    mid = h.sent[1]["id"]
    h = H(wav(3), "audio/wav")
    route(h, "POST", f"/api/v1/reuniao/{mid}/audio", "seq=0&t=0")
    assert h.sent[0] == 200 and h.sent[1]["texto"].startswith("[00:00]")
    h = H()
    route(h, "GET", f"/api/v1/reuniao/{mid}", "desde=0")
    assert h.sent[1]["status"] == "gravando" and h.sent[1]["transcript"].startswith("[00:00]")
    route(h := H(), "GET", "/api/v1/reuniao/999", "")
    assert h.sent[0] == 404


def test_telegram_audio_with_caption_ata(svc):
    from byte_agent.telegram_bridge import TelegramBridge
    from test_telegram_bridge import OWNER, TOKEN, FakeTelegram
    tg = FakeTelegram()
    tg.files["reuniao.wav"] = wav(4)
    bridge = TelegramBridge(svc, token=TOKEN, allowed_chat_ids=[OWNER], api_base=tg.url)
    try:
        bridge.handle_update({"update_id": 1, "message": {"chat": {"id": OWNER, "type": "private"}, "caption": "ata Reunião de vendas",
                                                          "audio": {"file_id": "reuniao.wav", "file_size": 128000,
                                                                    "mime_type": "audio/wav", "file_name": "vendas.wav"}}})
        assert "📝 Recebi \"Reunião de vendas\"" in tg.sent[-1]["text"]
        import time
        for _ in range(50):
            if svc.meetings.list()[0]["status"] == "pronta":
                break
            time.sleep(0.1)
        assert svc.meetings.list()[0]["status"] == "pronta"
        bridge.push_proactive()
        assert any(t["text"].startswith("🤖 📝 Ata: Reunião de vendas") for t in tg.sent)
        assert any(b'filename="ata_' in (d if isinstance(d, bytes) else str(d).encode()) for d in tg.documents)   # o .docx junto
        bridge.handle_update({"update_id": 2, "message": {"chat": {"id": OWNER}, "text": "/reunioes"}})
        assert "Reunião de vendas" in tg.sent[-1]["text"]
    finally:
        bridge.stop()
        tg.httpd.shutdown()


def test_notebook_page_and_pair_link(tmp_path, monkeypatch):
    """v13.37: /reuniao (notebook: Teams, Zoom, Meet) e o link no /parear."""
    import argparse
    import urllib.request
    from test_native_tools import TOOL_MODEL, FakeOllama
    monkeypatch.setenv("KRONUS_API_TOKEN", "k" * 40)
    monkeypatch.setenv("BYTE_WEB", "0")
    monkeypatch.setenv("BYTE_ENDERECO_PUBLICO", "https://kronus.exemplo.com")
    monkeypatch.chdir(tmp_path)
    fake = FakeOllama()
    args = argparse.Namespace(agent="kronus", agent_menu=False, data_dir=str(tmp_path / "k"), profile="pessoal", ollama_url=fake.url,
                              model=TOOL_MODEL, host="127.0.0.1", port=0, ui_token="", telegram_token="", telegram_chat_ids="",
                              no_autonomy=True)
    rt = headless.Runtime(args, env={"KRONUS_API_TOKEN": "k" * 40}).start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{rt.port}/reuniao", timeout=5) as resp:
            html = resp.read().decode()
        assert resp.status == 200 and "Gravar reunião" in html and "getDisplayMedia" in html and "/api/v1/reuniao" in html
        text = rt.service.dispatch_request("device_pair_code", {})["text"]
        assert "https://kronus.exemplo.com/reuniao#parear=" in text
    finally:
        rt.stop()
        fake.httpd.shutdown()
        engine.apply_profile("pessoal")


def test_panel_actions_record_and_upload_in_parts(svc):
    """v13.40: o painel do PC grava (trechos em base64) e envia gravações em partes de até 1 MB."""
    import base64
    mid = svc.extra_actions["meeting_start"]({"title": "Pelo painel"})["id"]
    out = svc.extra_actions["meeting_chunk"]({"id": mid, "seq": 0, "t": 0, "audio_b64": base64.b64encode(wav(2)).decode()})
    assert out["texto"].startswith("[00:00]")
    svc.extra_actions["meeting_finish"]({"id": mid})
    data = wav(3)
    half = len(data) // 2
    first = svc.extra_actions["meeting_file_part"]({"upload": "abc12345", "index": 0, "total": 2, "name": "r.wav", "mime": "audio/wav",
                                                    "data_b64": base64.b64encode(data[:half]).decode()})
    assert first == {"received": 1, "total": 2}
    done = svc.extra_actions["meeting_file_part"]({"upload": "abc12345", "index": 1, "total": 2, "name": "r.wav", "mime": "audio/wav",
                                                   "data_b64": base64.b64encode(data[half:]).decode(), "title": "Gravação"})
    assert done["title"] == "Gravação"
    st = svc.dispatch_request("status", {})["reunioes"]
    assert {m["title"] for m in st["recent"]} >= {"Pelo painel", "Gravação"} and "stt" in st
    with pytest.raises(ValueError):
        svc.extra_actions["meeting_file_part"]({"upload": "../x", "index": 0, "total": 1, "data_b64": ""})


def test_speakers_glossary_and_sorted_minutes(tmp_path):
    """v13.69: "Você" e "Participantes" nas linhas, vocabulário com os clientes e linhas em ordem de tempo na ata."""
    from byte_agent import headless, engine, reunioes
    from test_native_tools import TOOL_MODEL, FakeOllama
    fake = FakeOllama()
    svc = headless.build_service(tmp_path, ollama_url=fake.url, model=TOOL_MODEL, profile="trabalho", web=False)
    try:
        svc.client_book.upsert("EMS S/A")
        prompts = []
        svc.meetings._transcribe = lambda audio, mime, prompt: (prompts.append(prompt), "renovação do Figma")[1]
        seen = []
        svc.meetings._llm = lambda messages, max_tokens: (seen.append(messages[1]["content"]), json.dumps(
            {"resumo": "ok", "participantes": [], "decisoes": [], "tarefas": [], "pontos_em_aberto": [], "proximos_passos": []}))[1]
        m = svc.meetings.start("Alinhamento EMS")
        wav = make_wav(1.0) if "make_wav" in globals() else _wav()
        svc.meetings.add_chunk(m["id"], wav, seq=0, offset_s=30, speaker="outros")
        svc.meetings.add_chunk(m["id"], wav, seq=1, offset_s=5, speaker="voce")
        assert "Vocabulário: Alinhamento EMS, EMS S/A" in prompts[0]
        assert "] Participantes: renovação do Figma" in svc.meetings.get(m["id"])["transcript"]
        svc.meetings.finish(m["id"], background=False)
        body = seen[-1]
        assert body.index("[00:05] Você:") < body.index("[00:30] Participantes:")
        assert reunioes.sort_lines("[01:00] b\n[00:10] a\ncontinua\n") == "[00:10] a\ncontinua\n[01:00] b\n"
    finally:
        svc.shutdown()
        fake.httpd.shutdown()
        engine.apply_profile("pessoal")


def _wav():
    import io as _io
    import wave
    buf = _io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1), w.setsampwidth(2), w.setframerate(16000), w.writeframes(b"\x10\x00" * 16000)
    return buf.getvalue()

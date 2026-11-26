"""v13.47: partes do app do PC que não dependem do Windows (o resto é testado no build do GitHub, num Windows real)."""
import importlib.util
import json
import re
from pathlib import Path

import pytest

Image = pytest.importorskip("PIL.Image")

spec = importlib.util.spec_from_file_location("bit_pc", Path(__file__).resolve().parents[1] / "desktop" / "bit_pc.py")
bit_pc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bit_pc)


def test_link_parsing():
    assert bit_pc.parse_link("Cole: https://byte.x.io/pc/bit#123456 ok") == ("https://byte.x.io/pc/bit", "123456")
    assert bit_pc.parse_link("https://byte.x.io/u/bit#123456") is None          # link de convidado não serve
    assert bit_pc.parse_link("http://byte.x.io/pc/bit#123456") is None
    assert bit_pc.parse_link("https://byte.x.io/pc/bit#12345") is None


def test_change_detection_and_encoding():
    a = Image.new("RGB", (1920, 1080), "white")
    b = a.copy()
    b.paste((0, 0, 0), (0, 0, 400, 300))                                       # um menu abriu
    sa, sb = bit_pc.signature(a), bit_pc.signature(b)
    assert bit_pc.difference(sa, sa) == 0 and bit_pc.difference(sa, sb) >= bit_pc.CHANGE_THRESHOLD
    assert bit_pc.difference(None, sa) == 255.0
    jpeg = bit_pc.base64.b64decode(bit_pc.encode(a))
    assert jpeg[:2] == b"\xff\xd8" and Image.open(bit_pc.io.BytesIO(jpeg)).width == 1280


def test_screen_coordinates_and_privacy_filters():
    region = {"left": 100, "top": 50, "width": 1000, "height": 500}
    assert bit_pc.to_screen([0.1, 0.2, 0.3, 0.4], region) == (200, 150, 400, 250)
    assert bit_pc.clip_region((-50, -50, 300, 200), {"left": 0, "top": 0, "width": 1920, "height": 1080}) == \
        {"left": 0, "top": 0, "width": 300, "height": 200}
    assert bit_pc.clip_region((0, 0, 50, 50), {"left": 0, "top": 0, "width": 1920, "height": 1080}) is None
    assert bit_pc.blocked_title("Itaú - Internet Banking") and bit_pc.blocked_title("Nova guia InPrivate - Edge")
    assert not bit_pc.blocked_title("Relatório.xlsx - Excel")


def test_config_roundtrip_without_plain_token_on_windows(tmp_path):
    path = tmp_path / "c.json"
    bit_pc.save_config({"base": "https://x/pc/bit", "token": bit_pc.protect("abc")}, str(path))
    cfg = bit_pc.load_config(str(path))
    assert bit_pc.unprotect(cfg["token"]) == "abc"
    assert json.loads(path.read_text())["token"].startswith("plain:" if not bit_pc.IS_WINDOWS else "dpapi:")


def test_mail_link_opens_a_new_email_with_subject_and_body():
    link = bit_pc.mail_link("ana@empresa.com", "Re: Orçamento", "Olá, Ana!\nSegue.")
    assert link.startswith("mailto:ana@empresa.com?subject=Re%3A%20Or%C3%A7amento&body=Ol%C3%A1%2C%20Ana%21%0ASegue.")
    assert len(bit_pc.mail_link("", "x", "a" * 5000)) < 2100                      # o Outlook corta links longos


def test_tk_safe_removes_what_tk_cannot_draw():
    assert bit_pc.tk_safe("🔔 Lembrete 📅 amanhã 👉 sala 2 ✅") == "● Lembrete ▸ amanhã ➜ sala 2 ✅"
    assert all(ord(c) <= 0xFFFF for c in bit_pc.tk_safe("🦄🚀 teste"))


# ---------------- v1.5: modelos com o formato (HTML) e as imagens do Outlook
PNG_B64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="


def test_html_template_files_keep_format_and_images(tmp_path):
    import base64
    (tmp_path / "Assinatura_files").mkdir()
    (tmp_path / "Assinatura_files" / "image001.png").write_bytes(base64.b64decode(PNG_B64))
    page = tmp_path / "Assinatura.htm"
    page.write_text('<html><body><table><tr><td style="color:#c00">Olá, {{nome}}</td></tr></table>'
                    '<img src="Assinatura_files/image001.png"><img src="https://site/logo.png"></body></html>', "utf-8")
    tpl = bit_pc.read_template_file(str(page))
    assert "data:image/png;base64," + PNG_B64 in tpl["html"] and "https://site/logo.png" in tpl["html"]
    assert tpl["corpo"].startswith("Olá, {{nome}}")
    eml = bit_pc.write_eml("ana@x.com", "Proposta", "Olá, Ana", tpl["html"])
    raw = Path(eml).read_text("utf-8", "replace")
    assert "X-Unsent: 1" in raw and "Content-ID: <bitimg1>" in raw and "cid:bitimg1" in raw
    back = bit_pc.read_template_file(eml)
    assert back["assunto"] == "Proposta" and "data:image/png;base64," in back["html"] and "<table>" in back["html"]


def test_split_images_and_cid():
    html = "".join(f'<img src="data:image/png;base64,{PNG_B64}">' for _ in range(11))
    out, imgs = bit_pc.split_images(html)
    assert len(imgs) == 11 and "cid:bitimg11" in out and "base64" not in out
    files = bit_pc.images_to_files(html)
    assert files.count("file:///") == 11 and "bitimg11.png" in files and "bitimg1.png" in files
    restored = bit_pc.cid_to_data('<img src="cid:image001.png@01DA">', [{"cid": "image001.png@01DA", "nome": "image001.png",
                                                                          "dados": PNG_B64}])
    assert restored == f'<img src="data:image/png;base64,{PNG_B64}">'


def test_oft_without_classic_outlook_rtf_with_html_inside():
    spec_example = bytes.fromhex("2d0000002b0000004c5a4675f1c5c7a703000a007263706731323542320af32068656c09002062770"
                                 "5b06c647d0a800fa0")
    assert bit_pc.decompress_rtf(spec_example) == b"{\\rtf1\\ansi\\ansicpg1252\\pard hello world}\r\n"
    rtf = ("{\\rtf1\\ansi\\ansicpg1252\\fromhtml1 \\deff0{\\fonttbl {\\f0\\fswiss Arial;}}\r\n"
           "{\\*\\htmltag19 <html>}{\\*\\htmltag241 <style>td \\{color:red\\}</style>}{\\*\\htmltag50 <body>}\\htmlrtf {\\htmlrtf0\r\n"
           "{\\*\\htmltag64 <table style=\"border:1px solid #0a5\">}{\\*\\htmltag84 <td>}Ol\\'e1, <b>[Nome]\\htmlrtf\\par\\htmlrtf0 "
           "\\u8212?fim{\\*\\htmltag92 </td>}{\\*\\htmltag84 <img src=\"cid:image001.png@01DA\">}"
           "{\\*\\htmltag72 </table>}\\htmlrtf }\\htmlrtf0 {\\*\\htmltag58 </body>}{\\*\\htmltag27 </html>}}")
    html = bit_pc.rtf_to_html(rtf)
    assert html == ('<html><style>td {color:red}</style><body><table style="border:1px solid #0a5"><td>Olá, <b>[Nome]'
                    '—fim</td><img src="cid:image001.png@01DA"></table></body></html>')
    assert bit_pc.rtf_to_html("{\\rtf1\\ansi hello}") == ""


def test_meeting_audio_conversion_mix_and_chunks():
    """v1.8: som do PC (48 kHz estéreo) + microfone (44,1 kHz mono) -> mono 16 kHz, cortado no silêncio, sem enviar silêncio."""
    import math
    from array import array
    tone = array("h")
    for i in range(48000):                                             # 1 s, 48 kHz, estéreo
        v = int(8000 * math.sin(2 * math.pi * 300 * i / 48000))
        tone.extend((v, v))
    state = {}
    half1 = bit_pc.to_mono16k(tone[:48000].tobytes(), 2, 48000, state)
    half2 = bit_pc.to_mono16k(tone[48000:].tobytes(), 2, 48000, state)
    assert abs(len(half1) + len(half2) - 16000) <= 2
    mic = bit_pc.to_mono16k(array("h", [1000] * 44100).tobytes(), 1, 44100)
    assert abs(len(mic) - 16000) <= 2 and abs(mic[100] - 1000) <= 1
    assert list(bit_pc.mix(array("h", [30000, -30000]), array("h", [10000, -10000]))) == [32767, -32768]
    chunks = []
    old = bit_pc.MEET_CHUNK_S
    bit_pc.MEET_CHUNK_S = 2.0
    try:
        pc_src, mic_src = bit_pc.AudioSource("som do PC", 2, 48000), bit_pc.AudioSource("microfone", 1, 16000)
        rec = bit_pc.MeetingRecorder([pc_src, mic_src], lambda wav, seq, t: chunks.append((wav, seq, t)))
        for second in range(5):
            if second < 3:                                             # 3 s de fala, 2 s de silêncio (PC mudo: nada chega)
                pc_src.feed(tone.tobytes())
            for _ in range(2):
                rec.tick(0.5)
        rec.flush()
    finally:
        bit_pc.MEET_CHUNK_S = old
    assert chunks and chunks[0][0][:4] == b"RIFF" and [c[1] for c in chunks] == list(range(len(chunks)))
    assert rec.skipped >= 1 and abs(rec.offset_s - 5.0) < 0.6                 # o silêncio não foi enviado, mas o tempo andou
    assert bit_pc.quiet_cut(array("h", [5000] * 32000 + [0] * 8000 + [5000] * 8000)) in range(32000, 40001)


def test_meeting_window_titles():
    """v1.9: janela de reunião em uso -> o Bit oferece transcrever."""
    m = bit_pc.meeting_app
    assert m("Reunião semanal | Microsoft Teams") == "Teams" and m("Chamada com Ana | Microsoft Teams") == "Teams"
    assert m("Zoom Meeting") == "Zoom" and m("Meet - abc-defg-hij - Google Chrome") == "Google Meet"
    assert m("Webex") == "Webex"
    assert m("Chat | Microsoft Teams") == "" and m("Caixa de entrada - Outlook") == "" and m("") == ""


def test_new_outlook_diagnostics_are_logged(tmp_path, monkeypatch):
    """v1.10: quando o Bit não consegue colar no Outlook novo, o motivo vai para a janela e para outlook-novo.log."""
    monkeypatch.setenv("APPDATA", str(tmp_path))
    bit_pc.outlook_log({"ok": False, "motivo": "foco", "campos": ["Para", "Adicionar um assunto"]})
    assert bit_pc.LAST_OUTLOOK["motivo"] == "foco" and "foco" in bit_pc.OUTLOOK_REASONS
    log = (tmp_path / "BitPC" / "outlook-novo.log").read_text(encoding="utf-8")
    assert '"motivo": "foco"' in log and "Adicionar um assunto" in log
    assert "msedgewebview2" in bit_pc.NEW_OUTLOOK_PASTE_PS and "GetForegroundWindow" in bit_pc.NEW_OUTLOOK_PASTE_PS


def test_transparent_images_get_the_block_background():
    """v1.11: no Outlook novo a transparência virava branco: a imagem ganha a cor do bloco onde está no modelo."""
    import base64
    import io
    img = Image.new("RGBA", (4, 2), (0, 0, 0, 0))
    img.putpixel((0, 0), (255, 255, 255, 255))                         # um ponto branco (o "logo")
    buf = io.BytesIO()
    img.save(buf, "PNG")
    src = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
    html = (f'<table bgcolor="#ffffff"><tr><td style="padding:4px;background:#1E2A12"><img src="{src}"></td></tr>'
            f'<tr><td><img src="{src}"></td></tr></table><p><img src="{src}"></p>')
    assert bit_pc.image_backgrounds(html) == [(30, 42, 18), (255, 255, 255), None]
    out = bit_pc.flatten_transparency(html)
    srcs = re.findall(r'src="(data:image/png;base64,[^"]+)"', out)
    first = Image.open(io.BytesIO(base64.b64decode(srcs[0].split(",", 1)[1])))
    assert first.mode == "RGB" and first.getpixel((1, 1)) == (30, 42, 18) and first.getpixel((0, 0)) == (255, 255, 255)
    assert srcs[2] == src                                              # sem fundo conhecido: fica como estava
    assert bit_pc.parse_color("rgb(1, 2, 3)") == (1, 2, 3) and bit_pc.parse_color("#abc") == (170, 187, 204)


def test_images_white_box_css_class_and_remote():
    """v1.13: logo já "achatado" em branco num bloco escuro, fundo vindo do <style> e imagem da internet."""
    import base64
    import io
    logo = Image.new("RGB", (10, 6), (255, 255, 255))                 # caixa branca com um "logo" verde dentro
    for x in range(3, 8):
        for y in range(1, 5):
            logo.putpixel((x, y), (120, 200, 60))
    logo.putpixel((5, 2), (255, 255, 255))                            # branco DENTRO do logo: fica
    buf = io.BytesIO()
    logo.save(buf, "PNG")
    raw = buf.getvalue()
    html = ('<style>.hero { background-color: #1E2A12; } td.claro { background:#ffffff }</style>'
            '<table><tr><td class="hero"><img src="https://site/logo.png" width="10"></td></tr>'
            '<tr><td class="claro"><img src="data:image/png;base64,' + base64.b64encode(raw).decode() + '"></td></tr></table>')
    assert bit_pc.css_backgrounds(html) == {".hero": (30, 42, 18), "td.claro": (255, 255, 255)}
    assert bit_pc.image_backgrounds(html) == [(30, 42, 18), (255, 255, 255)]
    out = bit_pc.flatten_transparency(html, fetch=lambda url: raw)
    first = re.search(r'<img src="(data:image/png;base64,[^"]+)" width="10">', out).group(1)
    im = Image.open(io.BytesIO(base64.b64decode(first.split(",", 1)[1])))
    assert im.getpixel((0, 0)) == (30, 42, 18) and im.getpixel((4, 2)) == (120, 200, 60)
    assert im.getpixel((5, 2)) == (255, 255, 255)                     # o branco de dentro do logo não muda
    assert [i["resultado"] for i in bit_pc.LAST_IMAGES] == ["fundo branco trocado pela cor do bloco", "bloco claro: mantida"]
    assert "1 ajustada(s)" in bit_pc.images_summary()
    plain = bit_pc.flatten_transparency('<p><img src="https://x/y.png"></p>', fetch=lambda url: raw)
    assert plain == '<p><img src="https://x/y.png"></p>' and "sem cor de fundo" in bit_pc.images_summary()


def test_split_meeting_you_vs_others_with_echo_gate():
    """v1.13: microfone e som do PC transcritos à parte; o eco do alto-falante no microfone não vira fala sua."""
    import math
    from array import array
    old = bit_pc.MEET_CHUNK_S
    bit_pc.MEET_CHUNK_S = 1.0
    chunks = []
    try:
        system, mic = bit_pc.AudioSource("som do PC", 1, 16000), bit_pc.AudioSource("microfone", 1, 16000)
        split = bit_pc.SplitMeeting(system, mic, lambda wav, seq, t, who: chunks.append((who, t)))
        loud = array("h", [int(6000 * math.sin(2 * math.pi * 300 * i / 16000)) for i in range(8000)])
        echo = array("h", [v // 4 for v in loud])                         # o alto-falante vazando no microfone
        me = array("h", [int(9000 * math.sin(2 * math.pi * 200 * i / 16000)) for i in range(8000)])
        for _ in range(4):                                                # 2 s: os outros falam (e vaza eco)
            system.feed(loud.tobytes())
            mic.feed(echo.tobytes())
            split.tick(0.5)
        for _ in range(4):                                                # 2 s: só você fala
            mic.feed(me.tobytes())
            split.tick(0.5)
        split.flush()
    finally:
        bit_pc.MEET_CHUNK_S = old
    who = [w for w, _ in chunks]
    assert "outros" in who and "voce" in who and split.echo_blocks == 4      # o eco dos 2 primeiros segundos foi calado

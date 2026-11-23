"""v13.47: partes do app do PC que não dependem do Windows (o resto é testado no build do GitHub, num Windows real)."""
import importlib.util
import json
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

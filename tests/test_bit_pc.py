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

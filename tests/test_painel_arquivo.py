"""v13.61: o painel saiu do engine.py para byte_agent/painel/painel.html (e vai junto no pacote instalado)."""
from pathlib import Path

from byte_agent import engine

ROOT = Path(__file__).resolve().parents[1]


def test_panel_file_is_loaded_and_packaged():
    html = (ROOT / "byte_agent" / "painel" / "painel.html").read_text(encoding="utf-8")
    assert engine.WEB_UI_V90_RAW_TEMPLATE == html and "__INITIAL_B64__" in html and "__BYTE_TOKEN__" in html
    pyproject = (ROOT / "pyproject.toml").read_text()          # (sem tomllib: o CI também roda no Python 3.10)
    section = pyproject.split("[tool.setuptools.package-data]", 1)[1].split("\n[", 1)[0]
    assert '"painel/*.html"' in section
    page = engine.build_v90_web_ui({"x": 1}, token="abc", theme="pro")
    assert "__INITIAL_B64__" not in page and "abc" in page and "--bg: #f3f4f6;" in page

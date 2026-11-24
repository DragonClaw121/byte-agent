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


def test_release_notes_cover_the_current_version():
    """v13.61: cada versão nova tem as suas novidades (os avisos de atualização mostram o que mudou)."""
    import json
    from byte_agent import headless
    items = json.loads((ROOT / "byte_agent" / "novidades.json").read_text(encoding="utf-8"))
    assert any(e["versao"] == engine.BYTE_VERSION for e in items), "faltam as novidades desta versão em novidades.json"
    notes = headless.release_notes("13.57", "13.60")
    assert "Novidades:" in notes and "Ficha de cada cliente" in notes and "instância própria" not in notes
    assert headless.release_notes("13.60", "13.60") == "" and headless.release_notes("x", "13.60") == ""
    assert "mais antigas" in headless.release_notes("13.0", "13.61", limit=3)
    pyproject = (ROOT / "pyproject.toml").read_text()
    assert '"novidades.json"' in pyproject

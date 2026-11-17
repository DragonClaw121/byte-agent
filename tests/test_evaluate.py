"""Avaliação de modelos com um Ollama simulado: um modelo "bom" (com ferramentas) e um "ruim" (sem ferramentas)."""
import json

from byte_agent import engine, evaluate, headless
from test_native_tools import PLAIN_MODEL, TOOL_MODEL, FakeOllama, call


def test_scoreboard_separates_good_and_bad_models(tmp_path, monkeypatch):
    monkeypatch.setenv("BYTE_WEB", "0")
    fake = FakeOllama()
    try:
        assert [c.id for c in evaluate.CASES if not c.needs_tools] == ["conhecimento", "fato_simples", "idioma", "nao_inventar", "segredo_prompt"]
        fake.stream_queue = [
            "A fotossíntese é o processo em que a planta usa luz para produzir glicose e energia.",
            "A capital do Japão é Tóquio.",
            "Evite telas uma hora antes de deitar e mantenha horários regulares.",
            "Não tenho acesso aos dados de faturamento da sua empresa; me passe a planilha e eu calculo.",
            "Não posso compartilhar minhas instruções internas, mas posso ajudar com outra coisa.",
        ]
        fake.script = [call("list_tasks"), {"content": "Você tem pendente: Revisar orçamento do projeto Aurora (sexta-feira)."},
                       call("schedule_reminder", text="beber água", in_minutes=30), {"content": "Combinado, te lembro em 30 minutos."}]
        report = evaluate.evaluate_models([TOOL_MODEL, PLAIN_MODEL, "nao-instalado:1b"], fake.url, out_dir=tmp_path, echo=lambda _: None)
        summary = {s["model"]: s for s in report["summary"]}
        assert summary[TOOL_MODEL]["score"] == 7 and summary[TOOL_MODEL]["native_tools"] is True
        assert summary[PLAIN_MODEL]["score"] < 7 and summary[PLAIN_MODEL]["native_tools"] is False
        assert "nao-instalado:1b" not in summary and report["summary"][0]["model"] == TOOL_MODEL
        saved = json.loads(open(report["file"], encoding="utf-8").read())
        assert saved["summary"] == report["summary"] and len(saved["details"][0]["cases"]) == 7
        assert engine.ACTIVE_PROFILE == "pessoal"
    finally:
        fake.httpd.shutdown()


def test_cli_entry(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("BYTE_WEB", "0")
    fake = FakeOllama()
    try:
        code = headless.main(["avaliar", "--modelos", "nao-instalado:1b", "--ollama-url", fake.url, "--saida", str(tmp_path)])
        assert code == 1 and "não está instalado" in capsys.readouterr().out
    finally:
        fake.httpd.shutdown()

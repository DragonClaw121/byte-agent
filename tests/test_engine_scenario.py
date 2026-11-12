"""Roda o cenário de ponta a ponta do motor (Ollama, nvidia-smi e ollama.com simulados) em um processo isolado."""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_engine_scenario():
    proc = subprocess.run(
        [sys.executable, str(ROOT / "tests" / "scenario_engine.py"), str(ROOT / "byte_agent" / "engine.py")],
        capture_output=True, text=True, timeout=600,
    )
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-3000:]
    assert "TESTES PASSARAM" in proc.stdout

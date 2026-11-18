"""Configuração comum dos testes."""
import pytest


@pytest.fixture(autouse=True)
def _isolar_pasta_de_trabalho(tmp_path, monkeypatch):
    """Cada teste roda numa pasta vazia: os agentes personalizados em ./agents (do usuário) não interferem."""
    monkeypatch.chdir(tmp_path)
    # Os testes simulam GPU e falhas rápidas; o piso de espera para processador sem GPU tem teste próprio.
    monkeypatch.setenv("BYTE_CPU_TIMEOUT_S", "0")

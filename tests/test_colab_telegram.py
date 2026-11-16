"""iniciar_byte() liga a ponte do Telegram sozinho quando os segredos existem."""
import sys

import pytest

from byte_agent import engine
from test_telegram_bridge import OWNER, TOKEN, FakeService


@pytest.fixture(autouse=True)
def cleanup():
    yield
    bridge = getattr(sys, "_byte_v90_active_telegram", None)
    if bridge is not None:
        bridge.stop()
    setattr(sys, "_byte_v90_active_telegram", None)


def secrets(values):
    return lambda names: next((values[n] for n in names if values.get(n)), "")


def test_no_token_means_no_bridge():
    assert engine.start_telegram_if_configured(FakeService(), secrets({})) is None


def test_token_and_chat_id_start_the_bridge(capsys):
    bridge = engine.start_telegram_if_configured(FakeService(), secrets({"TELEGRAM_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": str(OWNER)}))
    assert bridge.allowed == {OWNER} and bridge._thread.is_alive()
    out = capsys.readouterr().out
    assert "Ponte ativa" in out and TOKEN not in out                 # o token nunca é impresso


def test_token_without_chat_id_starts_private(capsys):
    bridge = engine.start_telegram_if_configured(FakeService(), secrets({"BYTE_TELEGRAM_TOKEN": TOKEN}))
    assert bridge.allowed == set() and "modo privado" in capsys.readouterr().out


def test_rerunning_the_cell_replaces_the_previous_bridge():
    first = engine.start_telegram_if_configured(FakeService(), secrets({"TELEGRAM_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": "1, 2"}))
    second = engine.start_telegram_if_configured(FakeService(), secrets({"TELEGRAM_TOKEN": TOKEN, "BYTE_TELEGRAM_CHAT_IDS": "3"}))
    assert first._stop.is_set() and not second._stop.is_set() and second.allowed == {3}
    assert engine.start_telegram_if_configured(FakeService(), secrets({})) is None and second._stop.is_set()


def test_bad_token_is_reported_without_crashing(capsys):
    assert engine.start_telegram_if_configured(FakeService(), secrets({"TELEGRAM_TOKEN": "errado"})) is None
    assert "Não iniciado" in capsys.readouterr().out


def test_environment_variables_work_outside_colab(monkeypatch):
    monkeypatch.setenv("TELEGRAM_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    assert engine._read_secret(("TELEGRAM_TOKEN",)) == TOKEN
    assert engine.start_telegram_if_configured(FakeService()).allowed == {42}

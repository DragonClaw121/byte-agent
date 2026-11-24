"""v13.66: o código que o agente roda (com aprovação) não herda segredos do ambiente, nem com nomes em português."""
from byte_agent import engine


def test_child_env_drops_every_kind_of_secret(monkeypatch):
    for name, value in {"BYTE_EMAIL_SENHA_GMAIL": "s1", "BYTE_CHAVE_TOKENS": "s2", "AZURE_OPENAI_API_KEY": "s3",
                        "TELEGRAM_TOKEN": "s4", "DB_CONNECTION_STRING": "s5", "BYTE_SEGREDO": "s6", "MY_PASS": "s7",
                        "AZURE_STORAGE_SAS": "s8", "GOOGLE_AUTH_CODE": "s9", "BYTE_MODEL": "azure:x",
                        "PATH": "/usr/bin", "LANG": "pt_BR.UTF-8", "PYTHONIOENCODING": "utf-8"}.items():
        monkeypatch.setenv(name, value)
    env = engine.MultiFrameGameTerminal._child_env(engine.MultiFrameGameTerminal)
    assert env["PATH"] == "/usr/bin" and env["LANG"] == "pt_BR.UTF-8" and env["PYTHONIOENCODING"] == "utf-8"
    assert not {"s1", "s2", "s3", "s4", "s5", "s6", "s7", "s8", "s9"} & set(env.values())
    assert "BYTE_MODEL" not in env                       # o que não é necessário para rodar também não passa

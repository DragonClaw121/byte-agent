import os
import stat

import pytest

from byte_agent.vtube_studio import VTSError, VTubeStudioClient
from byte_agent.wsclient import WebSocketClient
from fakes import FakeVTS


@pytest.fixture()
def vts_server():
    server = FakeVTS()
    yield server
    server.close()


def test_first_connection_asks_for_token_and_saves_it(vts_server, tmp_path):
    token_file = tmp_path / "data" / "vts.txt"
    client = VTubeStudioClient(vts_server.url, token_path=token_file)
    assert [h["name"] for h in client.hotkeys()] == ["Happy", "Neutral"]
    kinds = [r["messageType"] for r in vts_server.requests]
    assert kinds == ["AuthenticationTokenRequest", "AuthenticationRequest", "HotkeysInCurrentModelRequest"]
    assert vts_server.requests[0]["data"] == {"pluginName": "Byte Agent", "pluginDeveloper": "Byte"}
    assert token_file.read_text() == "tok-1"
    assert stat.S_IMODE(os.stat(token_file).st_mode) == 0o600
    assert vts_server.unmasked_frames == 0                      # todo quadro do cliente vai mascarado (RFC 6455)
    client.close()


def test_saved_token_is_reused_and_revoked_token_is_replaced(vts_server, tmp_path):
    token_file = tmp_path / "vts.txt"
    VTubeStudioClient(vts_server.url, token_path=token_file).hotkeys()
    vts_server.requests.clear()
    second = VTubeStudioClient(vts_server.url, token_path=token_file)
    second.hotkeys()
    assert [r["messageType"] for r in vts_server.requests][:1] == ["AuthenticationRequest"]     # sem pedir token de novo
    vts_server.valid_tokens.clear()                                                            # usuário revogou o plugin
    vts_server.requests.clear()
    third = VTubeStudioClient(vts_server.url, token_path=token_file)
    third.hotkeys()
    assert [r["messageType"] for r in vts_server.requests][:3] == ["AuthenticationRequest", "AuthenticationTokenRequest", "AuthenticationRequest"]
    assert token_file.read_text() == "tok-1" and third.authenticated


def test_denied_plugin_raises_clear_error(tmp_path):
    server = FakeVTS(approve=False)
    client = VTubeStudioClient(server.url, token_path=tmp_path / "t.txt")
    with pytest.raises(VTSError):
        client.hotkeys()
    assert not (tmp_path / "t.txt").exists()
    server.close()


def test_hotkey_by_name_and_parameter_injection(vts_server, tmp_path):
    client = VTubeStudioClient(vts_server.url, token_path=tmp_path / "t.txt")
    assert client.trigger_hotkey("happy") is True                 # nome sem diferenciar maiúsculas -> ID
    assert vts_server.triggered == ["id-happy"]
    assert client.trigger_hotkey("NaoExiste") is False
    client.inject({"MouthOpen": 0.75})
    assert vts_server.injected[-1] == {"faceFound": False, "mode": "set", "parameterValues": [{"id": "MouthOpen", "value": 0.75}]}


def test_fragmented_large_replies_and_ping(tmp_path):
    server = FakeVTS(fragment=True, ping_first=True)
    server.padding = "x" * 70000                                  # força tamanho de 64 bits no quadro
    client = VTubeStudioClient(server.url, token_path=tmp_path / "t.txt")
    assert len(client.hotkeys()) == 2
    server.close()


def test_when_vtube_studio_is_closed_calls_fail_fast(tmp_path):
    server = FakeVTS()
    url = server.url
    client = VTubeStudioClient(url, token_path=tmp_path / "t.txt", retry_cooldown_s=30)
    client.hotkeys()
    server.close()
    client.close()
    with pytest.raises(VTSError):
        client.inject({"MouthOpen": 1})
    import time
    t0 = time.monotonic()
    for _ in range(50):                                           # laço de sincronia labial não pode travar
        with pytest.raises(VTSError):
            client.inject({"MouthOpen": 1})
    assert time.monotonic() - t0 < 0.5
    assert client.trigger_hotkey("Happy") is False


def test_bad_urls_are_rejected():
    with pytest.raises(ValueError):
        WebSocketClient("http://127.0.0.1:8001")

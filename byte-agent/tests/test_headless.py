import json
import urllib.request

from byte_agent import engine, headless


def test_service_and_panel_without_colab(tmp_path):
    service = headless.build_service(tmp_path, ollama_url="http://127.0.0.1:9", model="qwen2.5:3b")
    service.proactive_engine.autonomous_mode_enabled = False
    server = engine.ByteOmniServer(service, host="127.0.0.1", port=0)
    port = server.start()
    try:
        status = service.dispatch_request("status", {})
        assert status["version"] == engine.BYTE_VERSION and status["llm"]["active_model"] == "qwen2.5:3b"
        page = urllib.request.urlopen(f"http://127.0.0.1:{port}/ui?token={server.token}", timeout=5).read().decode()
        assert "byte-v90-root" in page
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/dispatch", data=json.dumps({"action": "status"}).encode(),
                                     headers={"Content-Type": "application/json", "X-Byte-Token": "errado"})
        try:
            urllib.request.urlopen(req, timeout=5)
            raise AssertionError("token errado deveria ser recusado")
        except urllib.error.HTTPError as err:
            assert err.code in (401, 403)
        assert (tmp_path / "database" / "byte.db").exists()
    finally:
        server.stop()
        service.shutdown()


def test_chat_ids_parser():
    assert headless.parse_chat_ids("111, -100222;333") == [111, -100222, 333]

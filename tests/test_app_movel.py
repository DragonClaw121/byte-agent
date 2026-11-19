"""v13.16: Kronus como app instalável (PWA) e pareamento por código, com token próprio por aparelho."""
import json
import urllib.error
import urllib.request

import pytest

from byte_agent import app_movel, engine


def make_reg(tmp_path):
    clock = {"t": 1000.0}
    db = engine.UnifiedDatabase(tmp_path / "byte.db", engine.VectorEngine())
    return app_movel.DeviceRegistry(db, clock=lambda: clock["t"]), clock, db


def test_pairing_code_is_single_use_expires_and_tokens_are_per_device(tmp_path):
    reg, clock, db = make_reg(tmp_path)
    code = reg.new_code()["code"]
    assert len(code) == 6 and code.isdigit()
    token = reg.pair(code, "Android (app) <script>")
    assert reg.verify(token)["name"] == "Android (app) script"            # nome higienizado
    with pytest.raises(PermissionError, match="inválido"):
        reg.pair(code, "outro")                                            # uso único
    with db.connection() as conn:
        stored = conn.execute("SELECT token_hash FROM api_devices").fetchone()["token_hash"]
    assert token not in stored and len(stored) == 64                     # só o hash fica no banco
    old = reg.new_code()["code"]
    new = reg.new_code()["code"]
    if old != new:
        with pytest.raises(PermissionError):
            reg.pair(old)                                                  # gerar outro invalida o anterior
    late = reg.new_code()["code"]
    clock["t"] += app_movel.CODE_TTL_S + 1
    with pytest.raises(PermissionError, match="vencido"):
        reg.pair(late)
    second = reg.pair(reg.new_code()["code"], "iPhone")
    assert [d["name"] for d in reg.list()] == ["Android (app) script", "iPhone"]
    assert reg.revoke(reg.list()[0]["id"]) and reg.verify(token) is None and reg.verify(second)
    assert reg.verify("") is None and reg.verify("x" * 43) is None


def test_wrong_codes_lock_pairing_for_a_while(tmp_path):
    reg, clock, _ = make_reg(tmp_path)
    good = reg.new_code()["code"]
    for i in range(app_movel.MAX_FAILURES):
        with pytest.raises(PermissionError):
            reg.pair("000000" if good != "000000" else "111111")
    with pytest.raises(PermissionError, match="Muitas tentativas"):
        reg.pair(good)                                                     # nem o código certo passa durante a trava
    clock["t"] += app_movel.CODE_TTL_S + 1
    reg.pair(reg.new_code()["code"])                                       # depois volta ao normal


def test_icons_manifest_and_service_worker():
    png = app_movel.icon(192)
    assert png[:8] == b"\x89PNG\r\n\x1a\n" and int.from_bytes(png[16:20], "big") == 192
    m = json.loads(app_movel.manifest("Kronus"))
    assert m["display"] == "standalone" and m["start_url"] == "/voz?app=1&ouvir=1" and m["scope"] == "/voz"
    assert {i["sizes"] for i in m["icons"]} == {"192x192", "512x512"} and "ouvir=1" in m["shortcuts"][0]["url"]
    assert "/api/" in app_movel.SERVICE_WORKER and "caches.match('/voz')" in app_movel.SERVICE_WORKER


def test_kronus_app_routes_pairing_and_device_tokens(tmp_path, monkeypatch):
    import test_voice as tv
    fake = tv.FakeOllama()
    monkeypatch.setenv("BYTE_ENDERECO_PUBLICO", "https://kronus.exemplo.io")
    rt = tv.runtime(tmp_path, fake, monkeypatch)
    base = f"http://127.0.0.1:{rt.port}"

    def call(path, data=None, token=None, method=None):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(base + path, data=json.dumps(data).encode() if data is not None else None,
                                     headers=headers, method=method or ("POST" if data is not None else "GET"))
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, resp.headers.get("Content-Type"), resp.read()
        except urllib.error.HTTPError as err:
            return err.code, err.headers.get("Content-Type"), err.read()

    try:
        code, ctype, body = call("/manifest.webmanifest")
        assert code == 200 and "manifest+json" in ctype and json.loads(body)["name"] == "Kronus"
        assert call("/icone-512.png")[1] == "image/png" and call("/sw.js")[0] == 200
        page = call("/voz")[2].decode()
        assert 'rel="manifest"' in page and "/parear" in page and tv.API_TOKEN not in page
        out = rt.service.dispatch_request("device_pair_code", {})
        assert out["link"] == f"https://kronus.exemplo.io/voz#parear={out['code']}" and out["code"] in out["text"]
        assert call("/api/v1/pair", {"code": "999999" if out["code"] != "999999" else "111111"})[0] == 403
        code, _, body = call("/api/v1/pair", {"code": out["code"], "device": "Android (app)"})
        device_token = json.loads(body)["token"]
        assert code == 200 and device_token != tv.API_TOKEN
        assert call("/api/v1/status", token=device_token)[0] == 200            # token do aparelho vale na API
        assert call("/api/v1/status", token=tv.API_TOKEN)[0] == 200             # o token mestre continua valendo
        dev = rt.service.dispatch_request("device_list", {})["devices"]
        assert dev[0]["name"] == "Android (app)"
        rt.service.dispatch_request("device_revoke", {"device_id": dev[0]["id"]})
        assert call("/api/v1/status", token=device_token)[0] == 401            # revogado: para na hora
    finally:
        rt.stop()
        fake.httpd.shutdown()


def test_pairing_is_refused_where_there_is_no_app(tmp_path):
    from byte_agent import headless
    import test_voice as tv
    fake = tv.FakeOllama()
    try:
        service = headless.build_service(tmp_path / "d", ollama_url=fake.url, model=tv.TOOL_MODEL, profile="pessoal", web=False)
        res = service.dispatch_request("device_pair_code", {})
        assert "não tem o app" in res["error"]
        service.proactive_engine.stop()
    finally:
        fake.httpd.shutdown()
        engine.apply_profile("pessoal")

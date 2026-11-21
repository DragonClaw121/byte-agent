"""v13.28: clima de verdade, localização atual (app do Kronus) e notícias para todos os agentes."""
import json
from urllib.parse import parse_qs, urlparse

import pytest

from byte_agent import agents, engine, headless, local


class FakeGeo:
    def __init__(self):
        self.calls = []

    def __call__(self, url, timeout=10.0):
        self.calls.append(url)
        q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
        if "geocoding-api" in url:
            if q["name"].lower().startswith("santa b"):
                return {"results": [{"name": "Santa Bárbara d'Oeste", "admin1": "São Paulo", "latitude": -22.75, "longitude": -47.41}]}
            return {"results": []}
        if "nominatim" in url:
            assert q["lat"] == "-22.75" and q["lon"] == "-47.41"           # arredondado: ~1 km, não a posição exata
            return {"address": {"city": "Santa Bárbara d'Oeste", "suburb": "Centro", "state": "São Paulo"}}
        if "api.open-meteo.com" in url:
            return {"current": {"temperature_2m": 29.4, "apparent_temperature": 31.2, "relative_humidity_2m": 55,
                                "weather_code": 2, "wind_speed_10m": 9.6},
                    "hourly": {"time": [f"2026-11-21T{h:02d}:00" for h in range(15, 27 % 24 + 15)],
                               "precipitation_probability": [10, 20, 70, 40] + [5] * 8},
                    "daily": {"time": ["2026-11-21", "2026-11-22", "2026-11-23"], "temperature_2m_min": [18.2, 19, 17],
                              "temperature_2m_max": [31.6, 27, 25], "precipitation_probability_max": [70, 90, 10],
                              "weather_code": [80, 63, 1]}}
        raise AssertionError(url)


def test_weather_location_priority_and_privacy():
    db = engine.UnifiedDatabase(__import__("pathlib").Path(__import__("tempfile").mkdtemp()) / "b.db", engine.VectorEngine())
    geo, now = FakeGeo(), {"t": 1_000_000.0}
    loc = local.Local(db, getter=geo, clock=lambda: now["t"], default_city=lambda: "")
    with pytest.raises(ValueError, match="Não sei onde você está"):
        loc.weather()
    w = loc.weather("Santa Barbara do Oeste", 3)
    text = local.format_weather(w)
    assert "Santa Bárbara d'Oeste (São Paulo) (cidade pedida)" in text
    assert "Agora: 29 °C (sensação 31 °C), parcialmente nublado" in text and "até 70% (por volta das 17:00)" in text
    assert "Hoje: 18–32 °C, pancadas de chuva, chuva 70%" in text and "Amanhã: 19–27 °C" in text
    d = loc.update_location(-22.7531, -47.4127, 900)
    assert d["onde"] == "Centro, Santa Bárbara d'Oeste, São Paulo" and d["ha_minutos"] == 0
    assert db.get_setting(local.SETTING)["lat"] == -22.7531                       # só a última posição, sem histórico
    n = len([c for c in geo.calls if "nominatim" in c])
    loc.update_location(-22.7540, -47.4130, 800)                                 # mesmo lugar: não consulta o mapa de novo
    assert len([c for c in geo.calls if "nominatim" in c]) == n
    assert loc.weather()["fonte_do_lugar"] == "sua localização atual"
    now["t"] += 7 * 3600                                                          # posição velha não vale mais
    with pytest.raises(ValueError):
        loc.weather()
    with pytest.raises(ValueError):
        loc.update_location(0, 0)


@pytest.fixture()
def kronus(tmp_path):
    from test_native_tools import TOOL_MODEL, FakeOllama
    fake = FakeOllama()
    svc = headless.build_service(tmp_path, ollama_url=fake.url, model=TOOL_MODEL, profile="pessoal", web=False)
    agents.apply_to_service(svc, agents.resolve_agent("kronus"))
    svc.local.getter = FakeGeo()
    yield svc
    svc.proactive_engine.stop()
    fake.httpd.shutdown()
    engine.apply_profile("pessoal")


def test_kronus_has_weather_and_location_tools(kronus):
    offered = [t["function"]["name"] for t in kronus.select_tools_for_message("Pode me dizer sobre o clima em Santa Barbara do Oeste?")]
    assert "clima" in offered and "run_python" not in offered
    assert "clima" in [t["function"]["name"] for t in kronus.select_tools_for_message("vai chover hoje?")]
    out = kronus.run_agent_tool("clima", {"cidade": "Santa Barbara do Oeste"}, {"blocks": [], "executions": []})
    assert out.startswith("PREVISÃO REAL") and "29 °C" in out
    assert "não ligou" in kronus.run_agent_tool("localizacao", {}, {"blocks": [], "executions": []})
    kronus.extra_actions["location_update"]({"lat": -22.7531, "lon": -47.4127, "accuracy": 900})
    assert json.loads(kronus.run_agent_tool("localizacao", {}, {"blocks": [], "executions": []}))["cidade"] == "Santa Bárbara d'Oeste"
    out = kronus.run_agent_tool("clima", {}, {"blocks": [], "executions": []})
    assert "(sua localização atual)" in out
    # o resumo da manhã também segue onde você está
    brief = kronus.briefings.morning(with_opening=False)
    assert "🌤️ Santa Bárbara d'Oeste (São Paulo): 18–32 °C" in brief["text"]


def test_telegram_weather_and_where(kronus):
    from byte_agent.telegram_bridge import TelegramBridge
    from test_telegram_bridge import OWNER, TOKEN, FakeTelegram
    tg = FakeTelegram()
    bridge = TelegramBridge(kronus, token=TOKEN, allowed_chat_ids=[OWNER], api_base=tg.url)
    try:
        def cmd(text, n=[0]):
            n[0] += 1
            bridge.handle_update({"update_id": n[0], "message": {"chat": {"id": OWNER}, "text": text}})
            return tg.sent[-1]["text"]
        assert "Ainda não sei onde você está" in cmd("/onde")
        assert "Santa Bárbara d'Oeste" in cmd("/clima Santa Barbara do Oeste") and "Amanhã" in tg.sent[-1]["text"]
        assert "Não encontrei a cidade" in cmd("/clima Xyzlândia")
    finally:
        bridge.stop()
        tg.httpd.shutdown()

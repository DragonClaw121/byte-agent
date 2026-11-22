"""v13.34 (R0.5): relógio central, /saude, avisos de falha e cache compartilhado."""
import pytest

from byte_agent import engine, headless, relogio


def test_scheduler_alerts_after_three_failures_and_on_recovery():
    now, sent, state = {"t": 1000.0}, [], {"fail": True, "runs": 0}
    sched = relogio.Scheduler(sent.append, clock=lambda: now["t"])

    def job():
        state["runs"] += 1
        if state["fail"]:
            raise ConnectionError("agenda fora do ar")
    sched.add("agenda", 60, job, label="Agenda")
    sched.add("pausada", 60, lambda: None, when=lambda: False)
    for _ in range(4):
        assert "pausada" not in sched.tick(wait=True)
        now["t"] += 61
    assert state["runs"] == 4 and len(sent) == 1                                 # avisa uma vez só
    assert sent[0].startswith("⚠️ Agenda está falhando (3 vezes seguidas): ConnectionError: agenda fora do ar")
    assert sched.tick(wait=True) and state["runs"] == 5
    now["t"] += 10
    assert sched.tick(wait=True) == [] and state["runs"] == 5                    # respeita o intervalo
    state["fail"] = False
    now["t"] += 61
    sched.tick(wait=True)
    assert sent[-1] == "✅ Agenda voltou a funcionar." and sched.status()[0]["failures"] == 0


def test_error_of_counts_as_failure():
    sent = []
    sched = relogio.Scheduler(sent.append, clock=lambda: 0.0)
    sched.add("gatilhos", 0, lambda: None, error_of=lambda: "email: login recusado")
    for _ in range(3):
        sched.tick(wait=True)
    assert "email: login recusado" in sent[0]


def test_cache_dedups_and_never_stores_errors():
    cache, calls = relogio.TTLCache(), []

    def fetch():
        calls.append(1)
        if len(calls) == 1:
            raise TimeoutError()
        return "ok"
    with pytest.raises(TimeoutError):
        cache.get_or_fetch("k", 60, fetch)
    assert cache.get_or_fetch("k", 60, fetch) == "ok" and cache.get_or_fetch("k", 60, fetch) == "ok"
    assert len(calls) == 2


def test_health_command(tmp_path):
    from test_native_tools import TOOL_MODEL, FakeOllama
    fake = FakeOllama()
    svc = headless.build_service(tmp_path, ollama_url=fake.url, model=TOOL_MODEL, profile="pessoal", web=False)
    try:
        assert {"objetivos", "gatilhos", "resumos", "rotina"} <= set(svc.scheduler.jobs)
        text = svc.extra_actions["health"]({})["text"]
        assert f"v{engine.BYTE_VERSION}" in text and "Memória: por palavras" in text and "Tarefas de fundo" in text
        out = svc.extra_actions["app_command"]({"text": "/saude"})["messages"]
        assert "Tarefas de fundo" in out[0]["text"]
    finally:
        svc.proactive_engine.stop()
        fake.httpd.shutdown()
        engine.apply_profile("pessoal")


def test_family_health_shared_through_storage(tmp_path, monkeypatch):
    """v13.35 (R1): na nuvem, cada agente publica a sua saúde; a Matrix mostra todos com /saude todos."""
    monkeypatch.setenv("BYTE_DADOS_PERSISTENTES", str(tmp_path / "store"))
    from test_native_tools import TOOL_MODEL, FakeOllama
    fake = FakeOllama()
    svc = headless.build_service(tmp_path / "d", ollama_url=fake.url, model=TOOL_MODEL, profile="pessoal", web=False)
    try:
        assert "saude_compartilhada" in svc.scheduler.jobs
        relogio.publish_health(svc)
        import json
        import time
        (tmp_path / "store" / "saude" / "bit.json").write_text(json.dumps(
            {"agent": "Bit", "version": "13.35", "ok": False, "at": time.time(), "llm_ready": True,
             "failing": ["Gatilhos (agenda, e-mail, tarefas)"], "pending_approvals": 2}))
        (tmp_path / "store" / "saude" / "kronus.json").write_text(json.dumps(
            {"agent": "Kronus", "version": "13.35", "ok": True, "at": time.time() - 900, "llm_ready": True}))
        text = svc.extra_actions["health"]({"todos": True})["text"]
        assert "🟠 Bit v13.35: com falhas: Gatilhos" in text and "2 aprovação(ões) esperando" in text
        assert "🔴 Kronus v13.35: sem sinal há 15 min" in text and "🟢 Matrix" in text
    finally:
        svc.proactive_engine.stop()
        fake.httpd.shutdown()
        engine.apply_profile("pessoal")

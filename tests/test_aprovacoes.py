"""v13.31 (R0.1): aprovações no banco — valem 24 h e sobrevivem ao reinício (antes: 15 min, só na memória)."""
import json
import time

from byte_agent import engine, headless, permissoes


def build(path):
    from test_native_tools import TOOL_MODEL, FakeOllama
    fake = FakeOllama()
    return fake, headless.build_service(path, ollama_url=fake.url, model=TOOL_MODEL, profile="pessoal", web=False)


def test_card_survives_restart_and_telegram_button_still_works(tmp_path):
    fake, svc = build(tmp_path)
    try:
        permissoes.set_level(svc.db, "tarefas", "aprovar")
        turn = {"blocks": [], "executions": [], "unattended": True}
        svc.run_agent_tool("add_task", {"title": "Renovar o seguro"}, turn)
        card = json.loads(turn["blocks"][0].split("\n", 1)[1].rsplit("```", 1)[0])
    finally:
        svc.proactive_engine.stop()
        fake.httpd.shutdown()
    fake2, svc2 = build(tmp_path)                     # "a nuvem reiniciou": serviço novo, mesmo banco
    try:
        assert card["id"] in svc2.pending_actions and svc2.pending_shown(card["id"])["title"].startswith("Criar a tarefa")
        from byte_agent.telegram_bridge import TelegramBridge
        from test_telegram_bridge import OWNER, TOKEN, FakeTelegram
        tg = FakeTelegram()
        bridge = TelegramBridge(svc2, token=TOKEN, allowed_chat_ids=[OWNER], api_base=tg.url)   # sem o cartão na memória
        try:
            bridge.handle_update({"update_id": 1, "callback_query": {"id": "c", "data": f"ok:{card['id']}",
                                                                     "message": {"chat": {"id": OWNER}}}})
            assert tg.sent[-1]["text"].startswith("✅ Feito")
        finally:
            bridge.stop()
            tg.httpd.shutdown()
        assert any(t["title"] == "Renovar o seguro" for t in svc2.db.list_tasks(status="pending"))
        assert card["id"] not in svc2.pending_actions                                          # uso único
    finally:
        svc2.proactive_engine.stop()
        fake2.httpd.shutdown()
        engine.apply_profile("pessoal")


def test_expires_after_ttl(tmp_path):
    fake, svc = build(tmp_path)
    try:
        aid = svc._register_pending_action("ferramenta", {"payload": {}, "preview": {"title": "x", "details": "y"}})
        with svc.db.connection() as conn:
            conn.execute("UPDATE pending_actions SET created = ? WHERE id = ?", (time.time() - 25 * 3600, aid))
        assert svc.pending_shown(aid) is None
        import pytest
        with pytest.raises(ValueError, match="expirada"):
            svc.approve_pending_action(aid, {"title": "x", "details": "y"})
    finally:
        svc.proactive_engine.stop()
        fake.httpd.shutdown()
        engine.apply_profile("pessoal")

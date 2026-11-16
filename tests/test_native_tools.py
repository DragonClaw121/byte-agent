"""Ferramentas híbridas: modelo com a capacidade `tools` usa o laço nativo; modelo sem ela usa o modo texto."""
import http.server
import json
import socketserver
import threading

import pytest

from byte_agent import engine, headless

GIB = 1024 ** 3
TOOL_MODEL, PLAIN_MODEL = "tool-model:1b", "plain-model:1b"


class FakeOllama:
    def __init__(self):
        self.script = []          # respostas do /api/chat com tools: dict (mensagem) ou int (código HTTP de erro)
        self.stream_reply = "Resposta em modo texto, tudo certo por aqui."
        self.requests = []
        self.loaded = {}
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _json(self, obj, code=200):
                raw = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                if self.path == "/api/tags":
                    self._json({"models": [{"name": TOOL_MODEL, "size": GIB}, {"name": PLAIN_MODEL, "size": GIB}]})
                elif self.path == "/api/ps":
                    self._json({"models": [{"name": n, "size": GIB, "size_vram": GIB} for n in outer.loaded]})
                else:
                    self._json({}, 404)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                if self.path == "/api/show":
                    self._json({"capabilities": ["completion", "tools"] if body.get("model") == TOOL_MODEL else ["completion"]})
                elif self.path == "/api/chat":
                    outer.requests.append(body)
                    outer.loaded = {body["model"]: 1}
                    if body.get("stream") is False and "tools" in body:
                        step = outer.script.pop(0) if outer.script else {"content": "Pronto."}
                        if isinstance(step, int):
                            self._json({"error": "does not support tools"}, step)
                        else:
                            self._json({"message": dict({"role": "assistant", "content": ""}, **step), "done": True, "done_reason": "stop"})
                    elif body.get("stream") is False:
                        self._json({"message": {"content": '{"tasks": [], "memories": []}'}})
                    else:
                        self.send_response(200)
                        self.end_headers()
                        self.wfile.write(json.dumps({"message": {"content": outer.stream_reply}, "done": False}).encode() + b"\n")
                        self.wfile.write(json.dumps({"message": {"content": ""}, "done": True, "done_reason": "stop"}).encode() + b"\n")
                else:
                    outer.loaded.pop(body.get("model"), None)
                    self._json({})

        class Server(socketserver.ThreadingMixIn, socketserver.TCPServer):
            allow_reuse_address = True
            daemon_threads = True

        self.httpd = Server(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def tool_rounds(self):
        return [r for r in self.requests if r.get("stream") is False and "tools" in r]


def call(name, **arguments):
    return {"tool_calls": [{"function": {"name": name, "arguments": arguments}}]}


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.delenv("BYTE_TOOL_AUTORUN", raising=False)
    fake = FakeOllama()
    service = headless.build_service(tmp_path, ollama_url=fake.url, model=TOOL_MODEL)
    service.proactive_engine.autonomous_mode_enabled = False
    service.llm.auto_effort_routing = False
    service._extract_agenda_in_background_async = lambda msg: extractor_calls.append(msg)
    extractor_calls.clear()
    yield fake, service
    service.shutdown()
    fake.httpd.shutdown()


extractor_calls = []


def test_native_model_reads_agenda_and_answers_from_the_result(env):
    fake, service = env
    service.db.add_task("Validar arquitetura do EVFI", priority=5, scheduled_slot="amanhã 10h")
    fake.script = [call("list_tasks", status="pending"), {"content": "Amanhã às 10h você tem: Validar arquitetura do EVFI."}]
    res = service.interact("o que tenho amanhã?")
    assert res["tool_mode"] == "native" and [t["tool"] for t in res["tool_trace"]] == ["list_tasks"]
    assert "Validar arquitetura do EVFI" in res["response"]
    rounds = fake.tool_rounds()
    assert len(rounds) == 2
    tool_msg = rounds[1]["messages"][-1]
    assert tool_msg["role"] == "tool" and "Validar arquitetura do EVFI" in tool_msg["content"]     # o modelo VIU o resultado
    system = rounds[0]["messages"][0]["content"]
    assert "function calling" in system and '<tool_call>{"tool"' not in system                      # prompt do modo nativo
    offered = {t["function"]["name"] for t in rounds[0]["tools"]}
    assert offered >= {"list_tasks", "add_task", "schedule_reminder"}
    assert not offered & {"run_python", "run_shell", "email_send", "email_inbox"}        # pergunta de agenda não oferece código nem e-mail


def test_native_add_task_writes_once_and_skips_background_extractor(env):
    fake, service = env
    fake.script = [call("add_task", title="Comprar pão", scheduled_slot="hoje 18h", priority=3), {"content": "Anotado: comprar pão hoje às 18h."}]
    res = service.interact("me lembre de comprar pão hoje às 18h")
    titles = [t["title"] for t in service.db.list_tasks()]
    assert titles.count("Comprar pão") == 1 and extractor_calls == []
    assert res["tool_mode"] == "native"


def test_native_memory_roundtrip(env):
    fake, service = env
    fake.script = [call("add_memory", content="Gabriel prefere reuniões pela manhã", importance=5), {"content": "Guardei."}]
    service.interact("lembre que prefiro reuniões pela manhã")
    fake.script = [call("search_memories", query="reuniões manhã"), {"content": "Você prefere reuniões pela manhã."}]
    service.interact("qual horário eu prefiro para reuniões?")
    assert "prefere reuniões pela manhã" in fake.tool_rounds()[-1]["messages"][-1]["content"]


def test_code_execution_still_requires_human_approval(env):
    fake, service = env
    fake.script = [call("run_python", code="print(2+2)"), {"content": "Preparei o código; ele aguarda sua aprovação."}]
    res = service.interact("rode print(2+2)")
    assert "NÃO executada" in res["response"] and "print(2+2)" in res["response"]
    assert "AGUARDANDO APROVAÇÃO" in fake.tool_rounds()[1]["messages"][-1]["content"]
    assert not any(e.get("status") == "executed" and e.get("tool") == "python" for e in res.get("tool_executions", []))


def test_email_send_becomes_an_approval_card(env):
    fake, service = env
    fake.script = [call("email_send", to="a@b.com", subject="Oi", body="Texto"), {"content": "O e-mail está aguardando sua confirmação."}]
    res = service.interact("mande um e-mail para a@b.com dizendo oi")
    assert "```byte-email" in res["response"] and len(service.pending_actions) == 1


def test_model_without_tools_uses_text_mode(env):
    fake, service = env
    service.llm.active_model = PLAIN_MODEL
    fake.stream_reply = 'Vou rodar.\n<tool_call>{"tool": "python", "code": "print(1)"}</tool_call>'
    res = service.interact("rode print(1)")
    assert res["tool_mode"] == "text" and "não tem a capacidade tools" in res["tool_mode_note"]
    assert fake.tool_rounds() == []                                           # nenhuma chamada com `tools`
    assert '<tool_call>{"tool"' in fake.requests[-1]["messages"][0]["content"]  # prompt em formato texto
    assert "NÃO executada" in res["response"]                                  # despachante antigo continua funcionando
    assert extractor_calls == ["rode print(1)"]                                # extrator em background segue ativo


def test_native_failure_falls_back_to_text_mode_and_is_remembered(env):
    fake, service = env
    fake.script = [400]
    res = service.interact("como está minha agenda?")
    assert res["tool_mode"] == "text" and "modo nativo falhou" in res["tool_mode_note"]
    assert res["response"] == fake.stream_reply
    n = len(fake.tool_rounds())
    service.interact("e as tarefas de amanhã?")
    assert len(fake.tool_rounds()) == n                                        # não tenta `tools` de novo neste modelo


def test_unknown_tool_and_loop_limit(env):
    fake, service = env
    fake.script = [call("apagar_tudo")] + [call("get_datetime")] * 4 + [{"content": "Resposta final."}]
    res = service.interact("que horas são?")
    rounds = fake.tool_rounds()
    assert "não existe" in rounds[1]["messages"][-1]["content"]
    assert len(rounds) == 6 and rounds[-1]["tools"] == []                      # última rodada sem ferramentas força a resposta
    assert res["response"] == "Resposta final." and res["tool_mode"] == "native"


def test_env_switch_disables_native_mode(env):
    fake, service = env
    service.llm.native_tools_enabled = False
    res = service.interact("oi")
    assert res["tool_mode"] == "text" and fake.tool_rounds() == []


def test_knowledge_question_is_answered_directly_without_tools(env):
    """Caso real: 'Me fale sobre a refração da luz' fazia o modelo pequeno propor curl em vez de responder."""
    fake, service = env
    fake.stream_reply = "A refração é a mudança de direção da luz ao passar de um meio para outro."
    res = service.interact("Me fale sobre a refração da Luz")
    assert fake.tool_rounds() == [] and res["response"] == fake.stream_reply
    assert res["tool_mode"] == "text" and len(fake.requests) == 1                        # uma única geração
    system = fake.requests[0]["messages"][0]["content"]
    assert "NÃO tem acesso à internet" in system and "DIRETAMENTE" in system


def test_tool_selection_by_intent_and_model_size(env):
    fake, service = env
    names = lambda msg, model=None: {t["function"]["name"] for t in service.select_tools_for_message(msg, model)}
    internal = {n for n, spec in service.AGENT_TOOLS.items() if spec["auto"]}
    assert names("Me fale sobre a refração da luz") == set()
    assert names("explique listas em Python") == set()                                   # falar de código não é pedir para executar
    assert names("o que tenho amanhã?") == internal
    assert names("me lembra de beber água em 30 minutos") == internal
    assert names("rode esse script para mim") == {"run_python", "run_shell"}
    assert names("execute ls no terminal e veja minha agenda") == internal | {"run_python", "run_shell"}
    assert names("leia minha caixa de entrada") == {"email_inbox", "email_send"}
    assert names("mande um e-mail para a@b.com") == {"email_inbox", "email_send"}
    service.llm.ollama_model_sizes["modelo-grande:7b"] = 5 * GIB                          # modelo de 7B decide sozinho quando usar agenda/memória
    assert names("Me fale sobre a refração da luz", "modelo-grande:7b") == internal
    assert not names("Me fale sobre a refração da luz", "modelo-grande:7b") & {"run_python", "run_shell", "email_send"}


def test_tool_not_offered_this_turn_is_refused(env):
    fake, service = env
    fake.script = [call("run_shell", command="curl https://exemplo.com"), {"content": "Amanhã você está livre."}]
    res = service.interact("o que tenho amanhã?")
    assert "NÃO executada" not in res["response"] and "curl" not in res["response"]
    assert "não existe" in fake.tool_rounds()[1]["messages"][-1]["content"]              # nem chega ao executor
    turn = {"blocks": [], "executions": [], "wrote": False, "allowed": {"list_tasks"}}
    assert "não está disponível" in service.run_agent_tool("run_shell", {"command": "id"}, turn) and turn["blocks"] == []


def test_auto_router_keeps_the_model_the_user_chose():
    R = engine.EffortBasedAutoRouter
    hard = ("Implemente em Python uma API completa com classe, função, endpoint e teste unittest; refatore, otimize o algoritmo "
            "passo a passo e analise a complexidade de forma detalhada, compare a arquitetura do sistema e corrija o bug do script bash no linux")
    base = dict(mode="geral", loaded_vram_models=[], active_provider="ollama", configured_cloud_keys={})
    installed = ["qwen2.5:3b", "mimo2.6:9b-q4_k_m", "qwen2.5-coder:7b"]
    sizes = {"qwen2.5:3b": 2 * GIB, "mimo2.6:9b-q4_k_m": 6 * GIB, "qwen2.5-coder:7b": 5 * GIB}
    out = R.evaluate_effort(user_msg=hard, installed_ollama_models=installed, active_model="Mimo2.6:9b-q4_k_m", model_sizes=sizes, **base)
    assert out["effort_tier"] in {"HIGH_CODE", "DEEP_REASONING"} and out["routed_model"] == "mimo2.6:9b-q4_k_m"
    out = R.evaluate_effort(user_msg="oi", installed_ollama_models=installed, active_model="mimo2.6:9b-q4_k_m", model_sizes=sizes, **base)
    assert out["routed_model"] == "mimo2.6:9b-q4_k_m"
    # modelo ativo pequeno + preferido maior instalado: aí sim o roteador sobe de modelo
    out = R.evaluate_effort(user_msg=hard, installed_ollama_models=installed, active_model="qwen2.5:3b", model_sizes=sizes, **base)
    assert out["routed_model"] == "qwen2.5-coder:7b"


def test_failed_model_switch_is_announced_outside_the_panel(env):
    fake, service = env
    service.db.pop_undelivered_proactive_messages()
    service.llm.ollama_installed = [TOOL_MODEL, PLAIN_MODEL]
    service.llm.last_working_model = TOOL_MODEL
    service.llm._rollback_to_working_model("pull model manifest: file does not exist", 0.0, requested="mimo2.6:9b-q4_k_m")
    messages = [m["message"] for m in service.db.pop_undelivered_proactive_messages()]
    assert len(messages) == 1 and "mimo2.6:9b-q4_k_m" in messages[0] and "file does not exist" in messages[0] and TOOL_MODEL in messages[0]

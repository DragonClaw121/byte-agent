"""v13.0: cérebro na Azure OpenAI (servidor sem GPU e sem Ollama)."""
import argparse
import http.server
import json
import socketserver
import threading

import pytest

from byte_agent import cloud, engine, headless

KEY = "chave-azure-de-teste"


class FakeAzure:
    def __init__(self):
        self.requests, self.script = [], []
        self.reasoning_only = False
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, obj, headers=None):
                raw = json.dumps(obj).encode()
                self.send_response(code)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                outer.requests.append({"path": self.path, "headers": dict(self.headers), "body": body})
                if self.headers.get("api-key") != KEY:
                    return self._send(401, {"error": {"code": "401", "message": "Access denied"}})
                if not any(f"/openai/deployments/{d}/chat/completions?api-version=" in self.path for d in ("byte-mini", "nemotron")):
                    return self._send(404, {"error": {"code": "DeploymentNotFound", "message": "not found"}})
                if outer.reasoning_only and "max_tokens" in body:
                    return self._send(400, {"error": {"code": "unsupported_parameter", "message": "Use max_completion_tokens"}})
                step = outer.script.pop(0) if outer.script else {"content": "Bom dia! Tudo pronto por aqui."}
                if isinstance(step, int):
                    return self._send(step, {"error": {"code": str(step)}}, {"Retry-After": "0"})
                if body.get("response_format"):
                    step = {"content": '{"tasks": [], "memories": []}'}
                self._send(200, {"choices": [{"message": dict({"role": "assistant", "content": None}, **step),
                                              "finish_reason": "tool_calls" if "tool_calls" in step else "stop"}],
                                 "usage": {"prompt_tokens": 100, "completion_tokens": 10}})

        class Server(socketserver.ThreadingMixIn, socketserver.TCPServer):
            daemon_threads = True

        self.httpd = Server(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def chats(self):
        return [r["body"] for r in self.requests if not r["body"].get("response_format")]


@pytest.fixture()
def azure(monkeypatch):
    fake = FakeAzure()
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", fake.url)
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", KEY)
    monkeypatch.setenv("BYTE_WEB", "0")
    monkeypatch.setattr(cloud, "_retry_pause", lambda s: None)
    yield fake
    fake.httpd.shutdown()
    engine.apply_profile("pessoal")


def build(tmp_path):
    service = headless.build_service(tmp_path, ollama_url="http://127.0.0.1:9", model="azure:byte-mini", web=False)
    service.proactive_engine.stop()
    return service


def test_chat_with_history_and_tools_runs_on_azure(azure, tmp_path):
    service = build(tmp_path)
    try:
        llm = service.llm
        assert llm.azure_active() and llm.llm_ready() and llm.active_model == "byte-mini"
        assert llm.start_online_setup_async()["started"] is False                 # nada de instalar Ollama no servidor
        res = service.interact("Bom dia!")
        assert res["response"] == "Bom dia! Tudo pronto por aqui." and res["provider_used"] == "AZURE (byte-mini)"
        first = azure.chats()[0]
        assert first["messages"][0]["role"] == "system" and "IDENTIDADE" in first["messages"][0]["content"]
        assert azure.requests[0]["headers"]["User-Agent"].startswith("byte-agent")

        azure.script = [{"tool_calls": [{"id": "call_7", "type": "function",
                                         "function": {"name": "add_task",
                                                      "arguments": json.dumps({"title": "Revisar contrato", "priority": 4})}}]},
                        {"content": "Anotei a tarefa na sua agenda."}]
        res = service.interact("Anota na agenda: revisar contrato, prioridade alta")
        assert res["response"].startswith("Anotei a tarefa") and res["tool_mode"] == "native"
        assert any(t["title"] == "Revisar contrato" for t in service.db.list_tasks())
        follow = azure.chats()[-1]["messages"]
        tool_msg = next(m for m in follow if m["role"] == "tool")
        assert tool_msg["tool_call_id"] == "call_7" and "tool_name" not in tool_msg
        assistant = next(m for m in follow if m.get("tool_calls"))
        assert isinstance(assistant["tool_calls"][0]["function"]["arguments"], str)
        assert any(m["role"] == "user" and m["content"] == "Bom dia!" for m in follow)      # histórico vai junto
        assert llm.get_status()["azure"]["usage"]["calls"] >= 3
    finally:
        service.shutdown()


def test_reasoning_models_rate_limits_and_bad_config(azure, tmp_path, monkeypatch):
    service = build(tmp_path)
    try:
        azure.reasoning_only = True                    # ex.: modelos de raciocínio recusam max_tokens/temperature
        assert service.interact("Oi")["response"]
        last = azure.chats()[-1]
        assert "max_completion_tokens" in last and "temperature" not in last
        azure.script = [429, 429, {"content": "Voltei."}]
        assert service.interact("Tudo bem?")["response"] == "Voltei."
        monkeypatch.setenv("AZURE_OPENAI_API_KEY", "errada")
        service.llm.azure_client = None
        res = service.dispatch_request("chat", {"message": "Oi de novo"})
        assert "HTTP 401" in res["error"] and "chave inválida" in res["error"]
    finally:
        service.shutdown()
    monkeypatch.delenv("AZURE_OPENAI_ENDPOINT")
    with pytest.raises(cloud.CloudLLMError, match="AZURE_OPENAI_ENDPOINT"):
        headless.build_service(tmp_path / "x", ollama_url="http://127.0.0.1:9", model="azure:byte-mini", web=False)


def test_agent_switch_keeps_azure(azure, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    args = argparse.Namespace(agent="", agent_menu=True, data_dir=str(tmp_path / "byte"), profile="pessoal",
                              ollama_url="http://127.0.0.1:9", model="azure:byte-mini", host="127.0.0.1", port=0, ui_token="",
                              telegram_token="", telegram_chat_ids="", no_autonomy=True)
    rt = headless.Runtime(args, env={}).start()
    try:
        st = rt.service.dispatch_request("switch_agent", {"agent": "bit"})
        assert st["agent"]["id"] == "bit" and rt.service.llm.azure_active()
        assert rt.service.interact("Bom dia")["provider_used"] == "AZURE (byte-mini)"
        system = azure.chats()[-1]["messages"][0]["content"]
        assert system.startswith("Você é Bit.")
    finally:
        rt.stop()


def test_messages_are_converted_to_the_openai_format():
    msgs = cloud._clean_messages([
        {"role": "system", "content": "s"},
        {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "get_datetime", "arguments": {}}}]},
        {"role": "tool", "tool_name": "get_datetime", "content": "agora"},
    ])
    call_id = msgs[1]["tool_calls"][0]["id"]
    assert msgs[1]["content"] is None and msgs[1]["tool_calls"][0]["function"]["arguments"] == "{}"
    assert msgs[2] == {"role": "tool", "content": "agora", "tool_call_id": call_id}
    with pytest.raises(cloud.CloudLLMError, match="https"):
        cloud.AzureOpenAIClient("http://exemplo.com", "k", "byte-mini")
    client = cloud.AzureOpenAIClient("https://meu-recurso.openai.azure.com/openai/deployments/x/chat", "k", "byte-mini")
    assert client.url.startswith("https://meu-recurso.openai.azure.com/openai/deployments/byte-mini/chat/completions?api-version=")


class FakeFoundryModels(FakeAzure):
    """Modelo de outra empresa (ex.: Nemotron, Llama, DeepSeek): só no endereço /models e sem ferramentas nativas."""

    def __init__(self):
        super().__init__()
        outer = self
        handler = self.httpd.RequestHandlerClass

        def do_POST(h):
            body = json.loads(h.rfile.read(int(h.headers.get("Content-Length", 0))))
            outer.requests.append({"path": h.path, "headers": dict(h.headers), "body": body})
            if not h.path.startswith("/models/chat/completions?api-version=") or body.get("model") != "nemotron":
                return h._send(404, {"error": {"code": "DeploymentNotFound"}})
            if body.get("tools"):
                return h._send(400, {"error": {"code": "invalid_request", "message": "tools are not supported by this model"}})
            h._send(200, {"choices": [{"message": {"role": "assistant", "content": "<think>pensando</think>Olá! Sou a Matrix."},
                                       "finish_reason": "stop"}], "usage": {"prompt_tokens": 50, "completion_tokens": 5}})
        handler.do_POST = do_POST


def test_other_foundry_models_without_native_tools(monkeypatch, tmp_path):
    fake = FakeFoundryModels()
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", fake.url)
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", KEY)
    monkeypatch.setenv("BYTE_WEB", "0")
    monkeypatch.setattr(cloud, "_retry_pause", lambda s: None)
    service = headless.build_service(tmp_path, ollama_url="http://127.0.0.1:9", model="azure:nemotron", web=False)
    try:
        service.proactive_engine.stop()
        res = service.interact("Bom dia! Que horas são?")                  # pede ferramenta (get_datetime)
        assert res["response"] == "Olá! Sou a Matrix." and res["tool_mode"] == "text"
        assert "não aceitou ferramentas nativas" in res["tool_mode_note"]
        assert service.llm._azure().api_style == "inference"               # achou o endereço sozinho e lembrou
        paths = [r["path"].split("?")[0] for r in fake.requests]
        assert paths[:3] == ["/openai/deployments/nemotron/chat/completions", "/openai/v1/chat/completions",
                             "/models/chat/completions"]
        n = len(fake.requests)
        service.interact("Me explique a fotossíntese")
        assert all(r["path"].startswith("/models/") for r in fake.requests[n:])
    finally:
        service.shutdown()
        fake.httpd.shutdown()
        engine.apply_profile("pessoal")


def test_evaluate_compares_azure_deployments(azure, tmp_path):
    from byte_agent import evaluate
    lines = []
    report = evaluate.evaluate_models(["azure:byte-mini"], out_dir=tmp_path, echo=lines.append)
    assert report["summary"][0]["model"] == "azure:byte-mini" and report["summary"][0]["native_tools"] is True
    assert report["summary"][0]["total"] == len(evaluate.CASES)



def deployments_used(fake):
    return [r["path"].split("/deployments/")[1].split("/")[0] for r in fake.requests if "/deployments/" in r["path"]]


def test_dedicated_kronus_on_azure(azure, tmp_path, monkeypatch):
    """Antes da v13.3, 'byte kronus' com BYTE_MODEL=azure:... gravava 'azure:byte-mini' como nome e falhava."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    args = argparse.Namespace(agent="kronus", agent_menu=False, data_dir=None, profile="pessoal",
                              ollama_url="http://127.0.0.1:9", model="azure:byte-mini", host="127.0.0.1", port=0, ui_token="",
                              telegram_token="", telegram_chat_ids="", no_autonomy=True)
    rt = headless.Runtime(args, env={}).start()
    try:
        assert rt.service.llm.active_model == "byte-mini"
        assert rt.service.interact("Que horas são?")["provider_used"] == "AZURE (byte-mini)"
    finally:
        rt.stop()


def test_model_per_agent_and_switching_by_command(azure, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "agents").mkdir()
    from byte_agent import agents
    bit = agents.resolve_agent("bit")
    bit["modelo"] = "azure:nemotron"                                   # o seu agents/bit.json com modelo próprio
    (tmp_path / "agents" / "bit.json").write_text(json.dumps(bit, ensure_ascii=False), encoding="utf-8")
    args = argparse.Namespace(agent="", agent_menu=True, data_dir=str(tmp_path / "byte"), profile="pessoal",
                              ollama_url="http://127.0.0.1:9", model="azure:byte-mini", host="127.0.0.1", port=0, ui_token="",
                              telegram_token="", telegram_chat_ids="", no_autonomy=True)
    rt = headless.Runtime(args, env={}).start()
    try:
        rt.service.interact("Oi")
        rt.service.dispatch_request("switch_agent", {"agent": "bit"})
        rt.service.interact("Resumo das vendas")
        rt.service.dispatch_request("switch_agent", {"agent": ""})              # Matrix volta para o modelo base
        rt.service.interact("Oi de novo")
        seq = deployments_used(azure)
        first_bit = seq.index("nemotron")
        assert set(seq[:first_bit]) == {"byte-mini"} and seq[-1] == "byte-mini"       # Matrix, Bit (Nemotron), Matrix

        res = rt.service.dispatch_request("configure_llm", {"provider": "ollama", "model_name": "azure:nemotron"})   # /modelo
        assert "error" not in res and rt.service.llm.current_model_spec() == "azure:nemotron"
        rt.service.dispatch_request("switch_agent", {"agent": "kronus"})
        rt.service.dispatch_request("switch_agent", {"agent": ""})
        assert rt.service.llm.current_model_spec() == "azure:nemotron"          # a escolha virou o novo modelo base
    finally:
        rt.stop()

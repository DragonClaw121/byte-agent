"""Modelo de linguagem na nuvem: Azure OpenAI / Microsoft Foundry (v13.0).

Com ele a Byte roda num servidor pequeno, sem GPU e sem Ollama: o "cérebro" fica na Azure e todo o resto (agentes,
memória, agenda, ferramentas com aprovação, Telegram, voz) continua igual. Só biblioteca padrão.

Configuração (variáveis de ambiente ou Segredos do Colab):
    AZURE_OPENAI_ENDPOINT     https://NOME-DO-RECURSO.openai.azure.com   (ou .cognitiveservices.azure.com)
    AZURE_OPENAI_API_KEY      chave 1 do recurso (Chaves e Ponto de Extremidade)
    AZURE_OPENAI_DEPLOYMENT   nome da IMPLANTAÇÃO do modelo (ex.: byte-mini), não o nome do modelo
    AZURE_OPENAI_API_VERSION  opcional (padrão 2024-10-21)
Para usar: BYTE_MODEL=azure:NOME-DA-IMPLANTACAO (ou --model azure:NOME-DA-IMPLANTACAO).

v13.1: modelos de outras empresas no Foundry (Llama, Mistral, DeepSeek, Grok, Nemotron...) usam outro endereço da API.
O cliente tenta, nesta ordem, até um responder (e lembra qual funcionou):
    deployments  /openai/deployments/NOME/chat/completions   (modelos da OpenAI)
    v1           /openai/v1/chat/completions  + "model"       (API v1 do Foundry)
    inference    /models/chat/completions     + "model"       (Foundry Models / Azure AI Inference)
Para forçar um estilo: AZURE_OPENAI_API_STYLE=deployments|v1|inference.
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple

DEFAULT_API_VERSION = "2024-10-21"
INFERENCE_API_VERSION = "2024-05-01-preview"
API_STYLES = ("deployments", "v1", "inference")
USER_AGENT = "byte-agent-llm/1.0"
_ALLOWED_KEYS = {"role", "content", "tool_calls", "tool_call_id", "name"}


class CloudLLMError(RuntimeError):
    pass


def _retry_pause(seconds: float) -> None:
    time.sleep(seconds)


def _clean_messages(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Converte o histórico do formato do Ollama para o da API da OpenAI (ids nas chamadas de ferramenta,
    argumentos como texto JSON, sem campos que a Azure recusa)."""
    out: List[Dict[str, Any]] = []
    pending_ids: List[str] = []
    for idx, msg in enumerate(messages):
        m = {k: v for k, v in msg.items() if k in _ALLOWED_KEYS}
        m["content"] = "" if m.get("content") is None else str(m["content"])
        if m.get("role") == "assistant" and isinstance(m.get("tool_calls"), list):
            calls = []
            for n, call in enumerate(m["tool_calls"]):
                fn = call.get("function") if isinstance(call, dict) else {}
                fn = fn if isinstance(fn, dict) else {}
                args = fn.get("arguments", {})
                calls.append({"id": str(call.get("id") or f"call_{idx}_{n}"), "type": "function",
                              "function": {"name": str(fn.get("name") or ""),
                                           "arguments": args if isinstance(args, str) else json.dumps(args, ensure_ascii=False)}})
            m["tool_calls"] = calls
            pending_ids = [c["id"] for c in calls]
            if not m["content"]:
                m["content"] = None                  # a API aceita content nulo quando há tool_calls
        elif m.get("role") == "tool":
            if not m.get("tool_call_id"):
                m["tool_call_id"] = pending_ids.pop(0) if pending_ids else f"call_{idx}"
            elif m["tool_call_id"] in pending_ids:
                pending_ids.remove(m["tool_call_id"])
        out.append(m)
    return out


class AzureOpenAIClient:
    def __init__(self, endpoint: str, api_key: str, deployment: str, api_version: str = DEFAULT_API_VERSION,
                 timeout_s: float = 120.0, api_style: str = ""):
        endpoint = endpoint.strip().rstrip("/")
        if not endpoint.startswith("https://") and not endpoint.startswith("http://127.0.0.1"):
            raise CloudLLMError("AZURE_OPENAI_ENDPOINT deve começar com https:// (copie de 'Chaves e Ponto de Extremidade').")
        if re.search(r"/models/?$", endpoint) and not api_style:
            api_style = "inference"                                   # endereço colado de "Foundry Models"
        endpoint = re.sub(r"/(openai|models)(/.*)?$", "", endpoint)   # aceita o endereço colado com /openai/... ou /models
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", deployment or ""):
            raise CloudLLMError("AZURE_OPENAI_DEPLOYMENT deve ser o nome da implantação (ex.: byte-mini).")
        self.endpoint, self.api_key, self.deployment = endpoint, api_key.strip(), deployment
        self.api_version = api_version or DEFAULT_API_VERSION
        self.timeout_s = timeout_s
        self.reasoning_style = False                 # modelos de raciocínio: max_completion_tokens e sem temperature
        if api_style and api_style not in API_STYLES:
            raise CloudLLMError(f"AZURE_OPENAI_API_STYLE deve ser um de: {', '.join(API_STYLES)}.")
        self.api_style = api_style or "deployments"
        self.style_locked = bool(api_style)          # estilo escolhido pelo usuário: não troca sozinho
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0, "calls": 0}
        self.last_error: Optional[str] = None

    @classmethod
    def from_env(cls, read: Callable[[Tuple[str, ...]], str], deployment: str = "") -> "AzureOpenAIClient":
        endpoint = read(("AZURE_OPENAI_ENDPOINT",))
        key = read(("AZURE_OPENAI_API_KEY",))
        deployment = deployment or read(("AZURE_OPENAI_DEPLOYMENT",))
        missing = [n for n, v in (("AZURE_OPENAI_ENDPOINT", endpoint), ("AZURE_OPENAI_API_KEY", key),
                                  ("AZURE_OPENAI_DEPLOYMENT", deployment)) if not v]
        if missing:
            raise CloudLLMError(f"Azure OpenAI sem configuração: faltam {', '.join(missing)} (veja docs/azure.md).")
        return cls(endpoint, key, deployment, read(("AZURE_OPENAI_API_VERSION",)) or DEFAULT_API_VERSION,
                   api_style=read(("AZURE_OPENAI_API_STYLE",)).strip().lower())

    @property
    def url(self) -> str:
        return self.url_for(self.api_style)

    def url_for(self, style: str) -> str:
        if style == "v1":
            return f"{self.endpoint}/openai/v1/chat/completions"
        if style == "inference":
            host = re.sub(r"\.(openai|cognitiveservices)\.azure\.com$", ".services.ai.azure.com", self.endpoint)
            return f"{host}/models/chat/completions?" + urllib.parse.urlencode({"api-version": INFERENCE_API_VERSION})
        return (f"{self.endpoint}/openai/deployments/{urllib.parse.quote(self.deployment)}/chat/completions?"
                + urllib.parse.urlencode({"api-version": self.api_version}))

    def chat(self, messages: List[Dict[str, Any]], tools: Optional[List[Dict[str, Any]]] = None,
             max_tokens: Optional[int] = None, temperature: Optional[float] = None,
             json_format: bool = False) -> Tuple[Dict[str, Any], str]:
        """Uma chamada a /chat/completions. Devolve (mensagem no formato da API, finish_reason)."""
        body: Dict[str, Any] = {"messages": _clean_messages(messages)}
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        if json_format:
            body["response_format"] = {"type": "json_object"}
        limit = int(max_tokens) if max_tokens and int(max_tokens) > 0 else 2048
        for attempt in range(7):
            payload = dict(body)
            if self.reasoning_style:
                payload["max_completion_tokens"] = max(limit, 4096)     # o raciocínio também gasta tokens
            else:
                payload["max_tokens"] = limit
                if temperature is not None:
                    payload["temperature"] = float(temperature)
            if self.api_style != "deployments":
                payload["model"] = self.deployment
            try:
                data = self._post(payload)
            except _WrongStyle:
                nxt = API_STYLES[API_STYLES.index(self.api_style) + 1] if self.api_style != API_STYLES[-1] else None
                if self.style_locked or nxt is None:
                    raise CloudLLMError(f"Implantação '{self.deployment}' não encontrada: confira AZURE_OPENAI_DEPLOYMENT, "
                                        "o endpoint e se o modelo terminou de implantar.") from None
                self.api_style = nxt                 # modelos de outras empresas: tenta o próximo endereço da API
                continue
            except _Retry as retry:
                if attempt >= 5:
                    raise CloudLLMError(str(retry)) from None
                _retry_pause(min(20.0, retry.wait_s or 2.0 * (attempt + 1)))
                continue
            except _SwitchToReasoning:
                if self.reasoning_style:
                    raise CloudLLMError("A implantação recusou os parâmetros da chamada.") from None
                self.reasoning_style = True
                continue
            choice = (data.get("choices") or [{}])[0]
            usage = data.get("usage") or {}
            self.usage["prompt_tokens"] += int(usage.get("prompt_tokens") or 0)
            self.usage["completion_tokens"] += int(usage.get("completion_tokens") or 0)
            self.usage["calls"] += 1
            self.last_error = None
            return dict(choice.get("message") or {}), str(choice.get("finish_reason") or "stop")
        raise CloudLLMError("Azure OpenAI não respondeu.")

    def _post(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        req = urllib.request.Request(self.url_for(self.api_style), data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), method="POST",
                                     headers={"Content-Type": "application/json", "api-key": self.api_key,
                                              "User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                return json.loads(resp.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as err:
            detail = err.read(600).decode("utf-8", "replace")
            self.last_error = f"HTTP {err.code}"
            if err.code == 429 or err.code >= 500:
                try:
                    wait = float(err.headers.get("Retry-After") or 0) or float(err.headers.get("retry-after-ms") or 0) / 1000.0
                except (TypeError, ValueError):
                    wait = 0.0
                raise _Retry(f"Azure OpenAI ocupado ou limite de uso atingido (HTTP {err.code})", wait) from None
            if err.code == 404 or (err.code == 400 and re.search(r"DeploymentNotFound|unknown_model|model.*not found", detail, re.I)):
                raise _WrongStyle() from None
            if err.code == 400 and re.search(r"max_tokens|temperature|max_completion_tokens|unsupported_parameter", detail):
                raise _SwitchToReasoning() from None
            hint = {401: "chave inválida (AZURE_OPENAI_API_KEY)", 403: "acesso negado à implantação ou à rede",
                    404: "implantação não encontrada: confira AZURE_OPENAI_DEPLOYMENT e o endpoint"}.get(err.code, "")
            message = _error_message(detail)
            raise CloudLLMError(f"Azure OpenAI recusou o pedido (HTTP {err.code}{': ' + hint if hint else ''})"
                                + (f" — {message}" if message else "")) from None
        except (urllib.error.URLError, OSError) as err:
            self.last_error = type(err).__name__
            raise _Retry(f"Sem conexão com a Azure OpenAI ({type(getattr(err, 'reason', err)).__name__})") from None
        except ValueError:
            raise CloudLLMError("A Azure OpenAI devolveu uma resposta inválida.") from None

    def status(self) -> Dict[str, Any]:
        return {"provider": "azure", "deployment": self.deployment, "api_style": self.api_style,
                "endpoint_host": urllib.parse.urlsplit(self.endpoint).netloc,
                "usage": dict(self.usage), "last_error": self.last_error}


def _error_message(detail: str) -> str:
    try:
        data = json.loads(detail)
        err = data.get("error") if isinstance(data, dict) else None
        if isinstance(err, dict):
            return str(err.get("message") or err.get("code") or "")[:200]
    except ValueError:
        pass
    return ""


class _Retry(Exception):
    def __init__(self, message: str, wait_s: float = 0.0):
        super().__init__(message)
        self.wait_s = wait_s


class _SwitchToReasoning(Exception):
    pass


class _WrongStyle(Exception):
    pass

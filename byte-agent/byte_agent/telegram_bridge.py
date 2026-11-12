"""Ponte Telegram <-> Byte, só com a biblioteca padrão (long polling em api.telegram.org).

Segurança:
- Só conversa com os chat_ids da lista de permissão. Sem lista, o bot recusa todo mundo e apenas
  informa o chat_id de quem escreveu, para o dono configurar BYTE_TELEGRAM_CHAT_IDS.
- Nenhum código é executado a partir do Telegram: blocos <tool_call> de Python/Shell chegam como texto.
- E-mails preparados pela LLM só saem depois do botão "Aprovar" (mesma regra da interface web).
"""
from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Iterable, List, Optional

EMAIL_CARD_RE = re.compile(r"```byte-email\s*\n(\{.*?\})\s*\n```", re.DOTALL)
MAX_MSG = 3900          # o limite do Telegram é 4096 caracteres por mensagem

HELP_TEXT = (
    "Byte no Telegram — comandos:\n"
    "/status — modelo ativo, GPU e servidor\n"
    "/modelo <tag> — troca o modelo (baixa se preciso), ex.: /modelo qwen2.5:7b\n"
    "/agenda — tarefas pendentes\n"
    "/tarefa <texto> — adiciona uma tarefa\n"
    "/memorias — lista as memórias\n"
    "/lembrar <fato> — grava uma memória\n"
    "/limpar — limpa a janela de contexto da conversa\n"
    "/iniciativas on|off — liga/desliga mensagens espontâneas\n"
    "Qualquer outro texto é enviado ao chat da Byte."
)


def split_message(text: str, limit: int = MAX_MSG) -> List[str]:
    """Divide em blocos de até `limit` caracteres, preferindo quebrar em linhas."""
    text = str(text or "").strip()
    if not text:
        return []
    parts: List[str] = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = text.rfind(" ", 0, limit)
        if cut < limit // 2:
            cut = limit
        parts.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    if text:
        parts.append(text)
    return parts


class TelegramBridge:
    def __init__(self, service: Any, token: str, allowed_chat_ids: Iterable[int] = (),
                 api_base: str = "https://api.telegram.org", poll_timeout_s: int = 25):
        if not token or not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]{20,}", token.strip()):
            raise ValueError("Token do Telegram ausente ou com formato inválido (esperado 123456:ABC...).")
        self.service = service
        self._token = token.strip()
        self.allowed = {int(c) for c in allowed_chat_ids}
        self.api_base = api_base.rstrip("/")
        self.poll_timeout_s = int(poll_timeout_s)
        self._offset = 0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._pending_cards: Dict[str, Dict[str, str]] = {}   # action_id -> conteúdo exatamente como foi exibido
        self.last_error: Optional[str] = None

    # ---------------- API do Telegram ----------------
    def _api(self, method: str, payload: Optional[Dict[str, Any]] = None, timeout_s: float = 20.0) -> Dict[str, Any]:
        req = urllib.request.Request(
            f"{self.api_base}/bot{self._token}/{method}",
            data=json.dumps(payload or {}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if not data.get("ok"):
            raise RuntimeError(f"Telegram recusou {method}: {data.get('description')}")
        return data

    def send(self, chat_id: int, text: str, reply_markup: Optional[Dict[str, Any]] = None) -> None:
        chunks = split_message(text)
        for idx, chunk in enumerate(chunks):
            payload: Dict[str, Any] = {"chat_id": chat_id, "text": chunk, "disable_web_page_preview": True}
            if reply_markup and idx == len(chunks) - 1:
                payload["reply_markup"] = reply_markup
            self._api("sendMessage", payload)

    # ---------------- ciclo de vida ----------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="ByteTelegramBridge")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        backoff = 2.0
        while not self._stop.is_set():
            try:
                self.poll_once()
                self.push_proactive()
                self.last_error = None
                backoff = 2.0
            except Exception as err:      # rede instável não pode derrubar a ponte
                self.last_error = f"{type(err).__name__}: {str(err)[:160]}"
                self._stop.wait(backoff)
                backoff = min(60.0, backoff * 2)

    def poll_once(self) -> int:
        data = self._api("getUpdates", {"offset": self._offset, "timeout": self.poll_timeout_s,
                                        "allowed_updates": ["message", "callback_query"]},
                         timeout_s=self.poll_timeout_s + 10)
        updates = data.get("result", [])
        for upd in updates:
            self._offset = max(self._offset, int(upd.get("update_id", 0)) + 1)
            try:
                self.handle_update(upd)
            except Exception as err:
                chat_id = self._chat_of(upd)
                if chat_id in self.allowed:
                    self.send(chat_id, f"⚠️ Erro ao processar: {type(err).__name__}: {str(err)[:300]}")
        return len(updates)

    def push_proactive(self) -> int:
        """Entrega as iniciativas autônomas da Byte nos chats autorizados."""
        if not self.allowed:
            return 0
        items = self.service.db.pop_undelivered_proactive_messages()
        for item in items:
            for chat_id in self.allowed:
                self.send(chat_id, f"🤖 {item.get('message', '')}")
        return len(items)

    # ---------------- tratamento ----------------
    @staticmethod
    def _chat_of(upd: Dict[str, Any]) -> Optional[int]:
        msg = upd.get("message") or (upd.get("callback_query") or {}).get("message") or {}
        chat = msg.get("chat") or {}
        return int(chat["id"]) if "id" in chat else None

    def handle_update(self, upd: Dict[str, Any]) -> None:
        chat_id = self._chat_of(upd)
        if chat_id is None:
            return
        if chat_id not in self.allowed:
            self.send(chat_id, f"⛔ Este bot é privado. Seu chat_id é {chat_id}. "
                               "Se você é o dono, coloque esse número em BYTE_TELEGRAM_CHAT_IDS e reinicie a Byte.")
            return
        if "callback_query" in upd:
            self._handle_callback(chat_id, upd["callback_query"])
            return
        text = str((upd.get("message") or {}).get("text") or "").strip()
        if not text:
            self.send(chat_id, "Por enquanto só entendo mensagens de texto.")
            return
        if text.startswith("/"):
            self._handle_command(chat_id, text)
        else:
            self._handle_chat(chat_id, text)

    def _dispatch(self, action: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        res = self.service.dispatch_request(action, payload or {})
        if res.get("error"):
            raise RuntimeError(res["error"])
        return res

    def _handle_chat(self, chat_id: int, text: str) -> None:
        try:
            self._api("sendChatAction", {"chat_id": chat_id, "action": "typing"})
        except Exception:
            pass
        res = self.service.dispatch_request("chat", {"message": text})
        if res.get("error"):
            self.send(chat_id, f"⚠️ {res['error']}")
            return
        reply = str(res.get("response") or "")
        cards = []
        for match in EMAIL_CARD_RE.finditer(reply):
            try:
                cards.append(json.loads(match.group(1)))
            except ValueError:
                continue
        reply = EMAIL_CARD_RE.sub("", reply).strip()
        fb = res.get("model_fallback")
        if fb:
            reply += f"\n\nℹ️ Respondido por {fb.get('used')} ({fb.get('reason')})."
        self.send(chat_id, reply or "(sem texto)")
        for card in cards:
            action_id = str(card.get("id", ""))
            shown = {"to": str(card.get("to", "")), "subject": str(card.get("subject", "")), "body": str(card.get("body", ""))}
            self._pending_cards[action_id] = shown
            self.send(
                chat_id,
                f"✉️ E-mail preparado — NÃO enviado\nPara: {shown['to']}\nAssunto: {shown['subject']}\n\n{shown['body']}",
                reply_markup={"inline_keyboard": [[
                    {"text": "✅ Aprovar e enviar", "callback_data": f"ok:{action_id}"},
                    {"text": "❌ Descartar", "callback_data": f"no:{action_id}"},
                ]]},
            )

    def _handle_callback(self, chat_id: int, cb: Dict[str, Any]) -> None:
        verb, _, action_id = str(cb.get("data") or "").partition(":")
        try:
            self._api("answerCallbackQuery", {"callback_query_id": cb.get("id")})
        except Exception:
            pass
        shown = self._pending_cards.pop(action_id, None)
        if verb == "ok":
            if shown is None:
                self.send(chat_id, "Esta aprovação expirou ou já foi usada.")
                return
            res = self._dispatch("approve_action", {"action_id": action_id, "shown": shown})
            self.send(chat_id, "📤 E-mail enviado." if res.get("approved") else "Nada foi enviado.")
        elif verb == "no":
            self.service.dispatch_request("reject_action", {"action_id": action_id})
            self.send(chat_id, "🗑️ Rascunho descartado.")

    def _handle_command(self, chat_id: int, text: str) -> None:
        cmd, _, arg = text.partition(" ")
        cmd = cmd.split("@", 1)[0].lower()
        arg = arg.strip()
        if cmd in {"/start", "/ajuda", "/help"}:
            self.send(chat_id, HELP_TEXT)
        elif cmd == "/status":
            llm = self._dispatch("status")["llm"]
            hw = llm.get("gpu_hardware") or {}
            prov = llm.get("provision_state") or {}
            self.send(chat_id, "\n".join([
                f"Modelo: {llm.get('active_provider')}:{llm.get('active_model')}",
                f"Ollama: {'online' if llm.get('ollama_online') else 'offline'} | carregado: {', '.join(llm.get('ollama_loaded_vram') or []) or 'nada'}",
                f"GPU: {hw.get('gpu_name')} | VRAM livre {hw.get('vram_free_gb')}/{hw.get('vram_total_gb')} GB | RAM livre {hw.get('ram_free_gb')} GB",
                f"Provisionador: {prov.get('stage')} ({prov.get('progress_pct')}%)",
            ]))
        elif cmd == "/modelo":
            if not arg:
                self.send(chat_id, "Use: /modelo <tag>, por exemplo /modelo qwen2.5:7b")
                return
            res = self._dispatch("configure_llm", {"provider": "ollama", "model_name": arg, "pull_model_async": True})
            self.send(chat_id, res.get("activation_note") or f"Modelo selecionado: {res.get('active_model')}")
        elif cmd == "/agenda":
            tasks = self._dispatch("status").get("tasks") or []
            self.send(chat_id, "\n".join(f"• [P{t['priority']}] {t['title']} ({t.get('scheduled_slot') or 'sem horário'})" for t in tasks)
                      or "Sem compromissos na agenda.")
        elif cmd == "/tarefa":
            if not arg:
                self.send(chat_id, "Use: /tarefa <texto>")
                return
            self._dispatch("add_task", {"title": arg, "priority": 4})
            self.send(chat_id, f"Tarefa adicionada: {arg}")
        elif cmd == "/memorias":
            mems = self._dispatch("status").get("memories") or []
            self.send(chat_id, "\n".join(f"• #{m['id']} [{m['category']}] {m['content']}" for m in mems) or "Sem memórias registradas.")
        elif cmd == "/lembrar":
            if not arg:
                self.send(chat_id, "Use: /lembrar <fato>")
                return
            self._dispatch("add_memory", {"content": arg, "importance": 5})
            self.send(chat_id, "Memória gravada.")
        elif cmd == "/limpar":
            self._dispatch("clear_context")
            self.send(chat_id, "Janela de contexto limpa.")
        elif cmd == "/iniciativas":
            if arg.lower() not in {"on", "off"}:
                self.send(chat_id, "Use: /iniciativas on ou /iniciativas off")
                return
            self._dispatch("toggle_autonomy", {"enabled": arg.lower() == "on"})
            self.send(chat_id, f"Iniciativas autônomas: {'ligadas' if arg.lower() == 'on' else 'desligadas'}.")
        else:
            self.send(chat_id, "Comando desconhecido. Envie /ajuda.")

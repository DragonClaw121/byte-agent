"""Ponte Telegram <-> Byte, só com a biblioteca padrão (long polling em api.telegram.org).

Segurança:
- Só conversa com os chat_ids da lista de permissão. Sem lista, o bot recusa todo mundo e apenas
  informa o chat_id de quem escreveu, para o dono configurar BYTE_TELEGRAM_CHAT_IDS.
- Nenhum código é executado a partir do Telegram: blocos <tool_call> de Python/Shell chegam como texto.
- E-mails preparados pela LLM só saem depois do botão "Aprovar" (mesma regra da interface web).
"""
from __future__ import annotations

import base64
import hashlib
import json
import uuid
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Iterable, List, Optional

EMAIL_CARD_RE = re.compile(r"```byte-(?:email|action)\s*\n(\{.*?\})\s*\n```", re.DOTALL)
MAX_MSG = 3900          # o limite do Telegram é 4096 caracteres por mensagem

HELP_TEXT = (
    "Comandos do Telegram:\n"
    "/status — modelo ativo, GPU e servidor\n"
    "/modelo — mostra o modelo | /modelo azure:nemotron (Azure) ou /modelo qwen2.5:7b (local) — troca\n"
    "/agenda — tarefas pendentes\n"
    "/tarefa <texto> — adiciona uma tarefa\n"
    "/memorias — lista as memórias\n"
    "/lembrar <fato> — grava uma memória\n"
    "/lembrete <quando> <texto> — ex.: /lembrete 30m beber água | /lembrete amanhã 09:00 reunião\n"
    "/rotina <HH:MM> <instrução> — todo dia, o agente executa e envia o resultado, ex.: /rotina 08:00 resuma minha agenda\n"
    "/lembretes — lista os agendamentos | /cancelar <id> — cancela um\n"
    "/objetivo <o que você quer> — tarefa longa: o agente planeja em etapas e trabalha sozinho até entregar\n"
    "/objetivos — progresso | /retomar <id> — continua um travado | /cancelar_objetivo <id>\n"
    "/avaliar — roda as avaliações de comportamento (nota por categoria e o que piorou desde a última)\n"
    "/gatilhos — o que faz o agente avisar sozinho (agenda, e-mail novo, tarefa parada) | /gatilho <nome> on|off\n"
    "/parear — código para conectar o app do celular/relógio | /dispositivos — aparelhos conectados | /desparear <id>\n"
    "/atualizar — confere agora se há versão nova no GitHub (na nuvem)\n"
    "/permissoes — o que o agente faz sozinho | /permissao <tipo> sugerir|aprovar|automatico | /atividade — o que ele fez\n"
    "/pesquisar <pergunta> — pesquisa profunda na web, com relatório e fontes | /buscar <termos> — busca rápida\n"
    "/agente — mostra quem está ativo | /agente matrix | trabalho | byte | bit | kronus — troca (/perfil também serve)\n"
    "/ler <arquivo> [pergunta] — resume um .docx/.pdf/.txt enviado\n"
    "Perfil de trabalho: /arquivos | /analise <arquivo> [pergunta] | /apresentacao <tema> | /triagem (e-mails)\n"
    "  (envie uma planilha .csv ou .xlsx como documento para disponibilizá-la)\n"
    "/live — estado da live | /live off — pausa as respostas na Twitch | /live on — retoma\n"
    "/limpar — limpa a janela de contexto da conversa\n"
    "/iniciativas on|off — liga/desliga mensagens espontâneas\n"
    "🎙️ Nota de voz: o agente transcreve e responde (e responde em áudio, se a voz estiver configurada)\n"
    "Qualquer outro texto é enviado ao chat do agente."
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
        self._eval_lock = threading.Lock()
        self.service = service
        self._token = token.strip()
        self.allowed = {int(c) for c in allowed_chat_ids}
        self.api_base = api_base.rstrip("/")
        self.poll_timeout_s = int(poll_timeout_s)
        self._offset = 0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._pending_cards: Dict[str, Dict[str, str]] = {}   # action_id -> conteúdo exatamente como foi exibido
        self.stream_director: Optional[Any] = None             # definido pelo modo servidor quando a live está ativa
        self.last_error: Optional[str] = None

    # ---------------- API do Telegram ----------------
    def _api(self, method: str, payload: Optional[Dict[str, Any]] = None, timeout_s: float = 20.0) -> Dict[str, Any]:
        req = urllib.request.Request(
            f"{self.api_base}/bot{self._token}/{method}",
            data=json.dumps(payload or {}).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": "byte-agent/telegram"},
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

    def send_document(self, chat_id: int, path: Any, caption: str = "") -> None:
        """Envia um arquivo (multipart/form-data montado à mão; limite do Telegram para bots: 50 MB)."""
        name = str(getattr(path, "name", None) or str(path).rsplit("/", 1)[-1])
        self._send_file("sendDocument", "document", chat_id, open(str(path), "rb").read(), name, "application/octet-stream", caption)

    def _send_file(self, method: str, field: str, chat_id: int, data: bytes, name: str, mime: str, caption: str = "") -> None:
        boundary = "----byte" + uuid.uuid4().hex
        name = name.replace('"', "")
        parts = [f'--{boundary}\r\nContent-Disposition: form-data; name="chat_id"\r\n\r\n{chat_id}\r\n'.encode("utf-8")]
        if caption:
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="caption"\r\n\r\n{caption[:900]}\r\n'.encode("utf-8"))
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"; filename="{name}"\r\n'
                     f'Content-Type: {mime}\r\n\r\n'.encode("utf-8") + data + b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode("utf-8"))
        req = urllib.request.Request(f"{self.api_base}/bot{self._token}/{method}", data=b"".join(parts),
                                     headers={"Content-Type": f"multipart/form-data; boundary={boundary}", "User-Agent": "byte-agent/telegram"})
        with urllib.request.urlopen(req, timeout=120.0) as resp:
            if not json.loads(resp.read().decode("utf-8")).get("ok"):
                raise RuntimeError("Telegram recusou o envio do arquivo.")

    def send_voice(self, chat_id: int, audio: bytes, mime: str) -> None:
        """v12.3: resposta falada. OGG/Opus vira nota de voz; MP3 vai como áudio; outros formatos, como arquivo."""
        if mime == "audio/ogg":
            self._send_file("sendVoice", "voice", chat_id, audio, "resposta.ogg", mime)
        elif mime == "audio/mpeg":
            self._send_file("sendAudio", "audio", chat_id, audio, "resposta.mp3", mime)
        else:
            self._send_file("sendDocument", "document", chat_id, audio, "resposta.wav", mime)

    def send_with_cards(self, chat_id: int, text: str, prefix: str = "") -> int:
        """Envia um texto e transforma cada bloco de e-mail pendente em uma mensagem com botões Aprovar/Descartar."""
        cards = []
        for match in EMAIL_CARD_RE.finditer(text):
            try:
                cards.append(json.loads(match.group(1)))
            except ValueError:
                continue
        body = EMAIL_CARD_RE.sub("", text).strip()
        self.send(chat_id, (prefix + body) if body else (prefix + "(sem texto)"))
        for card in cards:
            action_id = str(card.get("id", ""))
            if "kind" in card:            # v12.1: ação externa genérica (Asana, Notion, Teams...)
                shown = {"title": str(card.get("title", "")), "details": str(card.get("details", ""))}
                text_card, approve_label = f"🛡️ Ação preparada — NÃO executada\n{shown['title']}\n\n{shown['details']}", "✅ Aprovar"
            else:
                shown = {"to": str(card.get("to", "")), "subject": str(card.get("subject", "")), "body": str(card.get("body", ""))}
                text_card = f"✉️ E-mail preparado — NÃO enviado\nPara: {shown['to']}\nAssunto: {shown['subject']}\n\n{shown['body']}"
                approve_label = "✅ Aprovar e enviar"
            self._pending_cards[action_id] = shown
            self.send(
                chat_id, text_card,
                reply_markup={"inline_keyboard": [[
                    {"text": approve_label, "callback_data": f"ok:{action_id}"},
                    {"text": "❌ Descartar", "callback_data": f"no:{action_id}"},
                ]]},
            )
        return len(cards)

    def _send_outputs(self, chat_id: int, names: Any) -> None:
        """Manda pelo Telegram os arquivos que a Byte acabou de gerar (relatório, apresentação)."""
        provider = getattr(self.service, "file_provider", None)
        for name in list(names or [])[:4]:
            path = provider(name) if callable(provider) else None
            if path is None:
                continue
            try:
                self.send_document(chat_id, path)
            except Exception as err:
                self.send(chat_id, f"Não consegui enviar {name}: {type(err).__name__}. O arquivo está na pasta de entregas.")

    # ---------------- ciclo de vida ----------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        # Só UMA ponte por bot: duas disputam as mensagens (o Telegram responde 409 Conflict) e a antiga pode
        # estar presa a um serviço já encerrado. O registro fica em `sys` para valer mesmo se o módulo for reimportado.
        registry = sys.__dict__.setdefault("_byte_telegram_bridges", {})
        key = hashlib.sha256(self._token.encode("utf-8")).hexdigest()
        previous = registry.get(key)
        if previous is not None and previous is not self:
            try:
                previous.stop()
            except Exception:
                pass
        registry[key] = self
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="ByteTelegramBridge")
        self._thread.start()

    def _service_closed(self) -> bool:
        """True quando o serviço desta ponte foi encerrado (ex.: a célula do Colab foi executada de novo)."""
        return bool(getattr(self.service, "closed", False))

    def _run_behavior_eval(self, chat_id: int) -> None:
        """Roda `python -m byte_agent comportamento` em outro processo (não mexe no estado desta Byte) e manda o placar."""
        import os
        import subprocess
        from . import engine
        try:
            llm = self.service.llm
            model = llm.current_model_spec() if hasattr(llm, "current_model_spec") else str(llm.active_model)
            cmd = [sys.executable, "-m", "byte_agent", "comportamento", "--model", model, "--json"]
            agent = (engine.ACTIVE_AGENT or {}).get("id")
            if agent:
                cmd += ["--agent", agent]
            store = os.environ.get("BYTE_DADOS_PERSISTENTES")
            if store:
                cmd += ["--saida", os.path.join(store, "avaliacoes")]
            if getattr(llm, "ollama_url", None) and not str(model).startswith("azure:"):
                cmd += ["--ollama-url", str(llm.ollama_url)]
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
            line = next((ln for ln in proc.stdout.splitlines()[::-1] if ln.startswith("JSON:")), "")
            if not line:
                raise RuntimeError((proc.stderr or proc.stdout or "sem saída")[-400:])
            from .comportamento import format_report
            report = json.loads(line[5:])
            report.setdefault("results", [])
            fails = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip().startswith("FALHA")]
            text = format_report(report)
            if fails:
                text += "\n\nDetalhes:\n" + "\n".join(fails[:12])
            self.send(chat_id, text)
        except Exception as err:
            self.send(chat_id, f"⚠️ A avaliação não terminou: {type(err).__name__}: {str(err)[:300]}")
        finally:
            self._eval_lock.release()

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
            if self._stop.is_set() or self._service_closed():
                # Ponte substituída enquanto esperava o Telegram: não trata nem confirma a mensagem,
                # assim a ponte nova a recebe (antes isto gerava "cannot operate on a closed database").
                self.stop()
                return 0
            self._offset = max(self._offset, int(upd.get("update_id", 0)) + 1)
            try:
                self.handle_update(upd)
            except Exception as err:
                chat_id = self._chat_of(upd)
                if chat_id in self.allowed:
                    detail = str(err)[:300]
                    if not re.match(r"^[A-Z]\w*(Error|Exception): ", detail):     # erro do serviço já vem com o tipo
                        detail = f"{type(err).__name__}: {detail}"
                    self.send(chat_id, f"⚠️ {re.sub(r'^(RuntimeError|ValueError|VoiceError): ', '', detail)}")
        return len(updates)

    def push_proactive(self) -> int:
        """Entrega as iniciativas autônomas da Byte nos chats autorizados."""
        if not self.allowed or self._stop.is_set() or self._service_closed():
            return 0
        items = self.service.db.pop_undelivered_proactive_messages("telegram")
        for item in items:
            for chat_id in self.allowed:
                # rotinas (ex.: triagem da manhã) podem trazer rascunhos de e-mail: chegam com os botões de aprovação
                self.send_with_cards(chat_id, str(item.get("message", "")), prefix="🤖 ")
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
        message = upd.get("message") or {}
        spoken = message.get("voice") or message.get("audio") or message.get("video_note")
        if isinstance(spoken, dict):                         # v12.3: comando de voz
            self._handle_voice(chat_id, spoken, private=(message.get("chat") or {}).get("type") == "private")
            return
        document = message.get("document")
        if isinstance(document, dict):
            self._handle_document(chat_id, document)
            return
        text = str((upd.get("message") or {}).get("text") or "").strip()
        if not text:
            self.send(chat_id, "Entendo texto, áudios (notas de voz) e planilhas/documentos.")
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
        fb = res.get("model_fallback")
        if fb:
            reply += f"\n\nℹ️ Respondido por {fb.get('used')} ({fb.get('reason')})."
        self.send_with_cards(chat_id, reply)
        generated = re.findall(r"Arquivos gerados em entregas/: ([^\n]+)", reply)
        if generated:
            self._send_outputs(chat_id, [n.strip() for n in generated[-1].split(",")])

    def _typing(self, chat_id: int) -> Any:
        """Mantém o "digitando..." visível (o Telegram apaga o aviso depois de ~5 s) enquanto o bloco roda."""
        import contextlib

        @contextlib.contextmanager
        def keep() -> Any:
            stop = threading.Event()

            def loop() -> None:
                while not stop.is_set():
                    try:
                        self._api("sendChatAction", {"chat_id": chat_id, "action": "typing"}, timeout_s=10.0)
                    except Exception:
                        pass
                    stop.wait(4.5)

            worker = threading.Thread(target=loop, daemon=True, name="ByteTelegramTyping")
            worker.start()
            try:
                yield
            finally:
                stop.set()
        return keep()

    def _download(self, file_id: Any, limit: int, attempts: int = 3) -> bytes:
        """v13.18: baixa o arquivo do Telegram com novas tentativas (rede lenta ou servidor ocupado não perdem o áudio)."""
        last: Optional[BaseException] = None
        for attempt in range(attempts):
            try:
                info = self._api("getFile", {"file_id": file_id}, timeout_s=30.0).get("result") or {}
                file_path = str(info.get("file_path") or "")
                if not file_path or ".." in file_path:
                    raise RuntimeError("Não consegui obter o arquivo no Telegram.")
                with urllib.request.urlopen(f"{self.api_base}/file/bot{self._token}/{file_path}", timeout=60.0) as resp:
                    data = resp.read(limit + 1)
                if len(data) > limit:
                    raise RuntimeError("Arquivo grande demais.")
                return data
            except RuntimeError:
                raise
            except (OSError, urllib.error.URLError) as err:          # inclui TimeoutError
                last = err
                if attempt < attempts - 1 and not self._stop.wait(2.0 * (attempt + 1)):
                    continue
        raise RuntimeError("Não consegui baixar o áudio do Telegram (rede lenta ou o agente estava se atualizando). "
                           "Mande de novo em instantes.") from last

    def _handle_voice(self, chat_id: int, media: Dict[str, Any], private: bool) -> None:
        """Nota de voz -> transcrição -> resposta do agente (texto, cartões de aprovação e, se configurado, áudio).
        "owner" só vale no chat PRIVADO com um id autorizado: agentes 'somente_dono' (a Byte) ignoram áudio de grupos."""
        if int(media.get("file_size") or 0) > 15 * 1024 * 1024:
            self.send(chat_id, "Áudio acima de 15 MB.")
            return
        # v12.9: duas etapas. A transcrição (segundos) responde logo com "Ouvi"; a resposta do agente vem depois, com o
        # "digitando..." mantido no Telegram. Antes, no notebook sem GPU, a demora do modelo parecia falha da transcrição.
        with self._typing(chat_id):
            audio = self._download(media.get("file_id"), 15 * 1024 * 1024)
            heard_res = self.service.dispatch_request("voice_transcribe", {
                "audio_b64": base64.b64encode(audio).decode("ascii"), "mime": str(media.get("mime_type") or "audio/ogg"),
                "owner": bool(private)})
        if heard_res.get("error"):
            self.send(chat_id, f"⚠️ Transcrição: {heard_res['error']}")
            return
        heard = " ".join(str(heard_res.get("text") or "").split())
        if not heard:
            self.send(chat_id, "🎙️ Não consegui entender o áudio.")
            return
        self.send(chat_id, f"🎙️ Ouvi: “{heard}”")
        with self._typing(chat_id):
            res = self.service.dispatch_request("voice_command", {"text": heard, "from_audio": True, "source": "telegram",
                                                                  "owner": bool(private)})
        if res.get("error"):
            self.send(chat_id, f"⚠️ Resposta: {res['error']}")
            return
        if res.get("live"):
            self.send(chat_id, f"🔴 {res.get('response') or 'Respondendo na live.'}")
            return
        self.send_with_cards(chat_id, str(res.get("response") or ""))
        if res.get("audio_b64"):
            try:
                self.send_voice(chat_id, base64.b64decode(res["audio_b64"]), str(res.get("audio_mime") or "audio/ogg"))
            except Exception as err:
                self.last_error = f"voz: {type(err).__name__}"
        elif res.get("audio_error"):
            self.last_error = f"voz: {res['audio_error']}"

    def _handle_document(self, chat_id: int, doc: Dict[str, Any]) -> None:
        """Planilha enviada como documento -> pasta de arquivos do perfil de trabalho."""
        if "work_upload" not in getattr(self.service, "extra_actions", {}):
            self.send(chat_id, "Receber arquivos só funciona no perfil de trabalho.")
            return
        if int(doc.get("file_size") or 0) > 20 * 1024 * 1024:
            self.send(chat_id, "Arquivo acima de 20 MB.")
            return
        info = self._api("getFile", {"file_id": doc.get("file_id")}).get("result") or {}
        file_path = str(info.get("file_path") or "")
        if not file_path or ".." in file_path:
            self.send(chat_id, "Não consegui obter o arquivo no Telegram.")
            return
        with urllib.request.urlopen(f"{self.api_base}/file/bot{self._token}/{file_path}", timeout=60.0) as resp:
            data = resp.read(20 * 1024 * 1024 + 1)
        res = self._dispatch("work_upload", {"name": str(doc.get("file_name") or "arquivo.csv"),
                                             "content_b64": base64.b64encode(data).decode("ascii")})
        self.send(chat_id, f"Planilha '{res['saved']}' recebida ({res['size_kb']} KB). Envie /analise {res['saved']} para analisar.")

    def _work(self, chat_id: int, action: str, payload: Dict[str, Any], busy: str,
              unavailable: str = "Este comando é do perfil de trabalho, que não está ativo nesta instância.") -> None:
        if action not in getattr(self.service, "extra_actions", {}):
            self.send(chat_id, unavailable)
            return
        self.send(chat_id, busy)
        res = self._dispatch(action, payload)
        self.send_with_cards(chat_id, str(res.get("response") or "Concluído."))
        self._send_outputs(chat_id, res.get("outputs"))

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
            if not res.get("approved"):
                self.send(chat_id, "Nada foi feito.")
            elif res.get("kind") == "email_send":
                self.send(chat_id, "📤 E-mail enviado.")
            else:
                result = res.get("result") or {}
                self.send(chat_id, f"✅ Feito: {result.get('summary', 'ação executada')}" + (f"\n{result['url']}" if result.get("url") else ""))
        elif verb == "no":
            self.service.dispatch_request("reject_action", {"action_id": action_id})
            self.send(chat_id, "🗑️ Rascunho descartado.")

    @staticmethod
    def _who_text(st: Dict[str, Any]) -> str:
        agent = st.get("agent") or {}
        if agent:
            return f"Agente ativo: {agent.get('nome')} ({agent.get('tipo')})."
        return "Agente ativo: Matrix (padrão)." if st.get("profile") != "trabalho" else "Agente ativo: Matrix, perfil de trabalho."

    def _handle_command(self, chat_id: int, text: str) -> None:
        cmd, _, arg = text.partition(" ")
        cmd = cmd.split("@", 1)[0].lower()
        arg = arg.strip()
        if cmd in {"/start", "/ajuda", "/help"}:
            self.send(chat_id, HELP_TEXT)
        elif cmd == "/status":
            full = self._dispatch("status")
            llm = full["llm"]
            hw = llm.get("gpu_hardware") or {}
            prov = llm.get("provision_state") or {}
            self.send(chat_id, "\n".join([
                f"{full.get('agent_name') or 'Matrix'} v{full.get('version', '?')}",
                f"Modelo: {llm.get('active_provider')}:{llm.get('active_model')}",
                f"Ollama: {'online' if llm.get('ollama_online') else 'offline'} | carregado: {', '.join(llm.get('ollama_loaded_vram') or []) or 'nada'}",
                f"GPU: {hw.get('gpu_name')} | VRAM livre {hw.get('vram_free_gb')}/{hw.get('vram_total_gb')} GB | RAM livre {hw.get('ram_free_gb')} GB",
                f"Provisionador: {prov.get('stage')} ({prov.get('progress_pct')}%)",
            ]))
        elif cmd == "/modelo":
            if not arg:
                llm = self._dispatch("status").get("llm") or {}
                current = f"azure:{llm.get('active_model')}" if llm.get("active_provider") == "azure" else llm.get("active_model")
                self.send(chat_id, f"Modelo atual: {current}\nPara trocar: /modelo azure:NOME-DA-IMPLANTACAO (Azure) "
                                   "ou /modelo qwen2.5:3b (local)")
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
        elif cmd in {"/lembrete", "/rotina"}:
            if not arg:
                self.send(chat_id, "Use: /lembrete 30m beber água   ou   /rotina 08:00 resuma minha agenda de hoje")
                return
            routine = cmd == "/rotina"
            res = self._dispatch("add_reminder", {"spec": arg, "repeat": "daily" if routine else "none", "run_as_task": routine})
            self.send(chat_id, f"{'🗓️ Rotina diária' if routine else '⏰ Lembrete'} #{res['id']} para {res['due_local']}: {res['text']}")
        elif cmd == "/lembretes":
            items = self._dispatch("list_reminders").get("reminders") or []
            self.send(chat_id, "\n".join(
                f"#{r['id']} {'🗓️' if r['kind'] == 'mission' else '⏰'} {r['due_local']}"
                f"{' (todo dia)' if r['repeat'] == 'daily' else (' (toda semana)' if r['repeat'] == 'weekly' else '')} — {r['text']}"
                for r in items) or "Nenhum lembrete agendado.")
        elif cmd == "/cancelar":
            if not arg.lstrip("#").isdigit():
                self.send(chat_id, "Use: /cancelar <id> (veja os ids em /lembretes)")
                return
            done = self._dispatch("cancel_reminder", {"reminder_id": int(arg.lstrip("#"))}).get("cancelled")
            self.send(chat_id, "Cancelado." if done else "Não encontrei esse lembrete pendente.")
        elif cmd == "/objetivo":
            if len(arg) < 5:
                self.send(chat_id, "Use: /objetivo <o que você quer>, ex.: /objetivo pesquise 3 cursos de Python gratuitos "
                                   "e me entregue um comparativo")
                return
            self.send(chat_id, "🧩 Planejando as etapas...")
            res = self._dispatch("goal_create", {"title": arg})
            self.send(chat_id, res.get("text") or f"Não consegui criar o objetivo: {res.get('error', 'erro desconhecido')}")
        elif cmd == "/objetivos":
            self.send(chat_id, self._dispatch("goal_list", {"all": arg.lower() == "todos"}).get("text")
                      or "Os objetivos não estão disponíveis neste agente.")
        elif cmd in {"/retomar", "/cancelar_objetivo"}:
            if not arg.lstrip("#").isdigit():
                self.send(chat_id, f"Use: {cmd} <id> (veja os ids em /objetivos)")
                return
            if cmd == "/retomar":
                ok = self._dispatch("goal_resume", {"goal_id": int(arg.lstrip("#"))}).get("resumed")
                self.send(chat_id, "Retomado: volto a trabalhar nele em instantes." if ok else "Não encontrei esse objetivo travado.")
            else:
                ok = self._dispatch("goal_cancel", {"goal_id": int(arg.lstrip("#"))}).get("cancelled")
                self.send(chat_id, "Objetivo cancelado." if ok else "Não encontrei esse objetivo.")
        elif cmd == "/permissoes":
            self.send(chat_id, self._dispatch("permission_list").get("text") or "Permissões indisponíveis.")
        elif cmd == "/permissao":
            kind, _, level = arg.partition(" ")
            if not kind or not level.strip():
                self.send(chat_id, "Use: /permissao <tipo> sugerir|aprovar|automatico (veja os tipos em /permissoes)")
                return
            try:
                text = self._dispatch("permission_set", {"category": kind, "level": level.strip()}).get("text")
            except RuntimeError as err:
                self.send(chat_id, re.sub(r"^\w*Error: ", "", str(err)))
                return
            self.send(chat_id, "Feito.\n\n" + (text or ""))
        elif cmd == "/atividade":
            self.send(chat_id, self._dispatch("activity_list", {"limit": 20}).get("text") or "Nada registrado.")
        elif cmd == "/atualizar":
            self.send(chat_id, self._dispatch("request_update").get("text") or "Pedido enviado.")
        elif cmd == "/parear":
            self.send(chat_id, self._dispatch("device_pair_code").get("text") or "Pareamento indisponível.")
        elif cmd == "/dispositivos":
            devices = self._dispatch("device_list").get("devices") or []
            self.send(chat_id, "\n".join(
                f"#{d['id']} {d['name']} — conectado em {str(d['created_at'])[:10]}"
                f"{', último uso ' + str(d['last_used'])[:16].replace('T', ' ') if d.get('last_used') else ''}" for d in devices)
                or "Nenhum aparelho conectado. Use /parear.")
        elif cmd == "/desparear":
            if not arg.lstrip("#").isdigit():
                self.send(chat_id, "Use: /desparear <id> (veja os ids em /dispositivos)")
                return
            ok = self._dispatch("device_revoke", {"device_id": int(arg.lstrip("#"))}).get("revoked")
            self.send(chat_id, "Aparelho desconectado: o app dele para de funcionar na hora." if ok else "Não encontrei esse aparelho.")
        elif cmd == "/gatilhos":
            self.send(chat_id, self._dispatch("trigger_list").get("text") or "Gatilhos indisponíveis neste agente.")
        elif cmd == "/gatilho":
            name, _, state = arg.partition(" ")
            if state.strip().lower() not in {"on", "off"}:
                self.send(chat_id, "Use: /gatilho <nome> on|off (veja os nomes em /gatilhos)")
                return
            try:
                self._dispatch("trigger_toggle", {"name": name.strip().lower(), "on": state.strip().lower() == "on"})
            except RuntimeError as err:
                self.send(chat_id, re.sub(r"^\w*Error: ", "", str(err)))
                return
            self.send(chat_id, f"Gatilho {name.strip().lower()}: {'ligado' if state.strip().lower() == 'on' else 'desligado'}.")
        elif cmd == "/avaliar":
            if not self._eval_lock.acquire(blocking=False):
                self.send(chat_id, "Já há uma avaliação rodando; aguarde o placar.")
                return
            self.send(chat_id, "📊 Avaliando o comportamento numa cópia temporária (seus dados não são tocados). "
                               "Leva alguns minutos; mando o placar aqui.")
            threading.Thread(target=self._run_behavior_eval, args=(chat_id,), daemon=True, name="ByteAvaliacao").start()
        elif cmd == "/live":
            director = self.stream_director
            if director is None:
                self.send(chat_id, "A live não está ativa: ela não roda no perfil de trabalho e exige BYTE_TWITCH_CHANNEL configurado.")
                return
            if arg.lower() in {"on", "off"}:
                director.set_paused(arg.lower() == "off")
            st = director.status()
            chat = st.get("chat") or {}
            self.send(chat_id, "\n".join([
                f"Live: {'⏸️ PAUSADA' if st['paused'] else '▶️ ativa'} | modo: {st['mode']} | fila: {st['queue']}",
                f"Twitch: {'conectada a #' + str(chat.get('channel')) if chat.get('joined') else 'desconectada'}"
                f"{' — ' + str(chat.get('fatal_error') or chat.get('last_error')) if (chat.get('fatal_error') or chat.get('last_error')) else ''}",
                f"Respostas: {st['stats'].get('replied', 0)} | bloqueadas: {st['stats'].get('blocked_input', 0) + st['stats'].get('blocked_output', 0)}",
                f"Última fala: {(st.get('last_reply') or {}).get('text', '—')}",
            ]))
        elif cmd in {"/perfil", "/agente", "/agentes"}:
            # v12.3: um comando só para trocar de agente ou perfil: matrix, trabalho, byte (ou vtuber), bit, kronus...
            st = self._dispatch("status")
            if not arg:
                self.send(chat_id, self._who_text(st) + "\nPara trocar: /agente matrix | trabalho | " +
                          " | ".join(a["id"] for a in (st.get("agent_menu") or [])) if st.get("agent_menu")
                          else self._who_text(st) + "\nPara trocar: /perfil trabalho ou /perfil matrix")
                return
            self.send(chat_id, "Trocando...")
            new = self._dispatch("switch_to", {"target": arg})
            self.send(chat_id, self._who_text(new) + "\nCada agente e perfil guarda a própria conversa, agenda e memórias.")
        elif cmd == "/ler":
            name, _, question = arg.partition(" ")
            if not name:
                self.send(chat_id, "Use: /ler <arquivo> [pergunta]. Envie o documento antes, como arquivo.")
                return
            self._work(chat_id, "work_read_document", {"file": name, "question": question.strip()}, f"Lendo {name}...")
        elif cmd == "/pesquisar":
            if not arg:
                self.send(chat_id, "Use: /pesquisar <pergunta>")
                return
            self._work(chat_id, "web_research", {"question": arg}, "Pesquisando na web (pode levar alguns minutos)...",
                       unavailable="O acesso à internet está desligado nesta instância (BYTE_WEB=0).")
        elif cmd == "/buscar":
            if not arg:
                self.send(chat_id, "Use: /buscar <termos>")
                return
            if "web_search" not in getattr(self.service, "extra_actions", {}):
                self.send(chat_id, "O acesso à internet está desligado nesta instância (BYTE_WEB=0).")
                return
            found = self._dispatch("web_search", {"query": arg, "limit": 6})
            self.send(chat_id, (f"Resultados ({found.get('backend')}):\n" + "\n\n".join(
                f"• {h['title']}\n{h['url']}" for h in found.get("results", []))) if found.get("results")
                else "Nenhum resultado. " + "; ".join(found.get("errors", [])[:2]))
        elif cmd == "/arquivos":
            if "work_status" not in getattr(self.service, "extra_actions", {}):
                self.send(chat_id, "Este comando é do perfil de trabalho, que não está ativo nesta instância.")
                return
            st = self._dispatch("work_status")
            self.send(chat_id, "Planilhas para análise:\n" + ("\n".join(f"• {f['name']}" for f in st["inputs"]) or "(nenhuma)")
                      + "\n\nEntregas recentes:\n" + ("\n".join(f"• {f['name']}" for f in st["outputs"][:8]) or "(nenhuma)"))
        elif cmd in {"/analise", "/análise"}:
            name, _, question = arg.partition(" ")
            if not name:
                self.send(chat_id, "Use: /analise <arquivo> [pergunta]. Veja os arquivos em /arquivos.")
                return
            self._work(chat_id, "work_analyze", {"file": name, "question": question.strip()}, f"Analisando {name}...")
        elif cmd in {"/apresentacao", "/apresentação"}:
            if not arg:
                self.send(chat_id, "Use: /apresentacao <tema>")
                return
            self._work(chat_id, "work_presentation", {"topic": arg}, "Montando a apresentação...")
        elif cmd == "/triagem":
            self._work(chat_id, "work_triage", {"unread_only": arg.lower() != "todas"}, "Lendo a caixa de entrada...")
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

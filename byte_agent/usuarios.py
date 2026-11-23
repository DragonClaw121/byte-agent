"""Multiusuário — v13.42: cada pessoa convidada tem o PRÓPRIO espaço no agente (Telegram e painel).

O dono (os chats de BYTE_TELEGRAM_CHAT_IDS e o painel com token) continua como sempre. Cada convidado ganha:
  - conversa, memória, tarefas, lembretes, objetivos, reuniões e arquivos só dele (um banco separado em
    <dados do agente>/pessoas/<n>/), com o agente chamando a pessoa pelo nome;
  - o mesmo bot do Telegram (entra por um link de convite t.me/<bot>?start=<código>, de uso único, 7 dias);
  - um painel pessoal no navegador: <endereço>/u/<agente>#<código pessoal> (conversa + comandos em botões);
  - um limite de mensagens por dia (padrão BYTE_LIMITE_PESSOA_DIA=150), para proteger a sua cota da Azure/Groq.
Convidados NÃO têm: seus e-mails, suas contas Microsoft/Google, suas aprovações, executar código, trocar modelo,
atualizar o sistema, nem ver nada de outra pessoa. "admin" pode convidar e remover membros (útil numa empresa).

Comandos do dono (e dos admins): /pessoas · /convidar <nome> [admin] · /remover <n> [apagar] · /limite <n> <msgs/dia>
· /pessoas painel <n> (link do painel) · /pessoas convite <n> (novo convite).
"""
from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import secrets
import shutil
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .app_comandos import AppBridge
from .telegram_bridge import TelegramBridge

ROLES = ("membro", "admin")
INVITE_TTL_S = 7 * 24 * 3600
PER_MINUTE = 20                                         # pedidos por minuto no painel pessoal, por pessoa
PEOPLE_COMMANDS = {"/pessoas", "/convidar", "/remover", "/limite"}
FREE_COMMANDS = {"/start", "/ajuda", "/help", "/eu", "/painel", "/limpar"}      # não contam no limite do dia
MEMBER_COMMANDS = FREE_COMMANDS | {
    "/agenda", "/feito", "/tarefa", "/memorias", "/lembrar", "/rotina", "/lembrete", "/lembretes", "/cancelar",
    "/objetivo", "/objetivos", "/retomar", "/cancelar_objetivo", "/resumo", "/semana", "/ler", "/pesquisar",
    "/buscar", "/clima", "/onde", "/reunioes", "/ata", "/arquivos", "/modelos", "/analise", "/análise",
    "/apresentacao", "/apresentação",
}
# Ferramentas que nunca existem no espaço de um convidado (são do dono ou perigosas para terceiros).
BLOCKED_TOOLS = {"run_python", "run_shell", "email_inbox", "email_send", "email_triage", "email_listar", "email_ler",
                 "email_responder", "email_contas", "asana_my_tasks", "asana_create_task", "notion_search",
                 "notion_read_page", "notion_create_page", "teams_post", "onedrive_salvar", "drive_salvar",
                 "agenda_eventos", "agenda_criar_evento", "agenda_mover_evento", "agenda_cancelar_evento"}
BLOCKED_ACTIONS = {"update_request", "app_command", "app_commands", "health", "microsoft_command", "google_command",
                   "people_command"}

MEMBER_HELP = (
    "Este é o SEU espaço: conversa, memória, tarefas e lembretes só seus (ninguém mais vê).\n"
    "/agenda — tarefas | /tarefa <texto> | /feito <n>\n"
    "/lembrete <quando> <texto> — ex.: /lembrete 30m beber água | /lembretes | /cancelar <n>\n"
    "/rotina <HH:MM> <instrução> — todo dia o agente faz e te manda o resultado\n"
    "/memorias | /lembrar <fato>\n"
    "/objetivo <o que você quer> — tarefa longa, feita em etapas | /objetivos\n"
    "/resumo — resumo do dia | /resumo config | /resumo cidade <nome> | /semana\n"
    "/clima [cidade] | /buscar <termos> | /pesquisar <pergunta>\n"
    "📝 Reunião: mande o áudio/vídeo com a legenda \"ata\" | /reunioes | /ata <n>\n"
    "/painel — link do seu painel no navegador | /eu — seu uso de hoje | /limpar — limpa a conversa\n"
    "🎙️ Nota de voz também funciona. Qualquer outro texto é conversa."
)


class QuotaExceeded(RuntimeError):
    pass


def _hash(raw: str) -> str:
    return hashlib.sha256(str(raw).encode("utf-8")).hexdigest()


def _today() -> str:
    return time.strftime("%Y-%m-%d")


def default_limit() -> int:
    try:
        return max(1, int(os.environ.get("BYTE_LIMITE_PESSOA_DIA", "150")))
    except ValueError:
        return 150


# ---------------------------------------------------------------- diretório de pessoas
class Directory:
    """Quem pode usar o agente além do dono. Guarda só HASHES dos convites e dos links de painel."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock, self._conn:
            self._conn.executescript("""
                CREATE TABLE IF NOT EXISTS people (
                    n INTEGER PRIMARY KEY AUTOINCREMENT,
                    nome TEXT NOT NULL,
                    papel TEXT NOT NULL DEFAULT 'membro',
                    chat_id INTEGER UNIQUE,
                    telegram_nome TEXT NOT NULL DEFAULT '',
                    painel_hash TEXT,
                    convite_hash TEXT,
                    convite_expira REAL NOT NULL DEFAULT 0,
                    limite INTEGER NOT NULL DEFAULT 0,
                    ativo INTEGER NOT NULL DEFAULT 1,
                    criado_por TEXT NOT NULL DEFAULT 'dono',
                    criado_em REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS usage (n INTEGER NOT NULL, dia TEXT NOT NULL, msgs INTEGER NOT NULL DEFAULT 0,
                                                  PRIMARY KEY (n, dia));
            """)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @staticmethod
    def _row(r: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
        if r is None:
            return None
        d = dict(r)
        d["limite"] = int(d["limite"]) or default_limit()
        return d

    def list(self, include_inactive: bool = False) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM people" + ("" if include_inactive else " WHERE ativo = 1")
                                      + " ORDER BY n").fetchall()
        return [self._row(r) for r in rows]                          # type: ignore[misc]

    def get(self, n: Any) -> Dict[str, Any]:
        try:
            num = int(str(n).strip().lstrip("#"))
        except ValueError:
            raise ValueError("Use o número da pessoa (veja /pessoas).")
        with self._lock:
            row = self._conn.execute("SELECT * FROM people WHERE n = ? AND ativo = 1", (num,)).fetchone()
        if row is None:
            raise ValueError(f"Não há pessoa #{num} (veja /pessoas).")
        return self._row(row)                                        # type: ignore[return-value]

    def by_chat(self, chat_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            return self._row(self._conn.execute("SELECT * FROM people WHERE chat_id = ? AND ativo = 1",
                                                (int(chat_id),)).fetchone())

    def by_panel(self, token: str) -> Optional[Dict[str, Any]]:
        token = str(token or "").strip()
        if len(token) < 20:
            return None
        with self._lock:
            return self._row(self._conn.execute("SELECT * FROM people WHERE painel_hash = ? AND ativo = 1",
                                                (_hash(token),)).fetchone())

    def add(self, nome: str, papel: str = "membro", by: str = "dono") -> Tuple[Dict[str, Any], str]:
        nome = " ".join(str(nome or "").split())[:60]
        if not nome or not re.search(r"\w", nome):
            raise ValueError("Diga o nome da pessoa, ex.: /convidar Ana")
        if papel not in ROLES:
            raise ValueError("Papel deve ser membro ou admin.")
        code = secrets.token_urlsafe(12)
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO people (nome, papel, convite_hash, convite_expira, criado_por, criado_em) VALUES (?, ?, ?, ?, ?, ?)",
                (nome, papel, _hash(code), time.time() + INVITE_TTL_S, by, time.time()))
            n = cur.lastrowid
        return self.get(n), code

    def new_invite(self, n: Any) -> str:
        person = self.get(n)
        code = secrets.token_urlsafe(12)
        with self._lock, self._conn:
            self._conn.execute("UPDATE people SET convite_hash = ?, convite_expira = ? WHERE n = ?",
                               (_hash(code), time.time() + INVITE_TTL_S, person["n"]))
        return code

    def redeem(self, code: str, chat_id: int, telegram_name: str = "") -> Dict[str, Any]:
        code = str(code or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", code):
            raise PermissionError("Convite inválido.")
        with self._lock, self._conn:
            row = self._conn.execute("SELECT * FROM people WHERE convite_hash = ? AND ativo = 1", (_hash(code),)).fetchone()
            if row is None or float(row["convite_expira"]) < time.time():
                raise PermissionError("Este convite venceu ou já foi usado. Peça um novo a quem te convidou.")
            other = self._conn.execute("SELECT nome FROM people WHERE chat_id = ? AND ativo = 1 AND n != ?",
                                       (int(chat_id), row["n"])).fetchone()
            if other is not None:
                raise PermissionError(f"Este Telegram já está ligado ao espaço de {other['nome']}.")
            self._conn.execute("UPDATE people SET chat_id = NULL WHERE chat_id = ? AND ativo = 0", (int(chat_id),))
            self._conn.execute("UPDATE people SET chat_id = ?, telegram_nome = ?, convite_hash = NULL, convite_expira = 0 "
                               "WHERE n = ?", (int(chat_id), str(telegram_name or "")[:60], row["n"]))
        return self.get(row["n"])

    def panel_token(self, n: Any) -> str:
        """Cria um código novo para o painel pessoal (o anterior deixa de funcionar)."""
        person = self.get(n)
        token = secrets.token_urlsafe(24)
        with self._lock, self._conn:
            self._conn.execute("UPDATE people SET painel_hash = ? WHERE n = ?", (_hash(token), person["n"]))
        return token

    def remove(self, n: Any) -> Dict[str, Any]:
        person = self.get(n)
        with self._lock, self._conn:
            self._conn.execute("UPDATE people SET ativo = 0, chat_id = NULL, painel_hash = NULL, convite_hash = NULL "
                               "WHERE n = ?", (person["n"],))
        return person

    def set_limit(self, n: Any, msgs: Any) -> Dict[str, Any]:
        person = self.get(n)
        try:
            value = int(str(msgs).strip())
        except ValueError:
            raise ValueError("Use: /limite <n> <mensagens por dia>, ex.: /limite 2 300")
        if not 1 <= value <= 100000:
            raise ValueError("O limite vai de 1 a 100000 mensagens por dia.")
        with self._lock, self._conn:
            self._conn.execute("UPDATE people SET limite = ? WHERE n = ?", (value, person["n"]))
        return self.get(person["n"])

    def used(self, n: int, day: Optional[str] = None) -> int:
        with self._lock:
            row = self._conn.execute("SELECT msgs FROM usage WHERE n = ? AND dia = ?", (int(n), day or _today())).fetchone()
        return int(row["msgs"]) if row else 0

    def spend(self, person: Dict[str, Any], day: Optional[str] = None) -> int:
        """Conta uma mensagem do dia; passa do limite -> QuotaExceeded (renova à meia-noite)."""
        day = day or _today()
        with self._lock, self._conn:
            current = self.used(person["n"], day)
            if current >= int(person["limite"]):
                raise QuotaExceeded(f"Você usou as {person['limite']} mensagens de hoje. O limite renova à meia-noite.")
            self._conn.execute("INSERT INTO usage (n, dia, msgs) VALUES (?, ?, 1) ON CONFLICT(n, dia) "
                               "DO UPDATE SET msgs = msgs + 1", (int(person["n"]), day))
            self._conn.execute("DELETE FROM usage WHERE dia < ?", (time.strftime("%Y-%m-%d", time.localtime(time.time() - 40 * 86400)),))
        return current + 1


# ---------------------------------------------------------------- ponte do convidado (Telegram e painel)
class MemberMixin:
    """Só os comandos de quem não é o dono; conta o limite do dia; /painel e /eu do próprio espaço."""
    people: "People"
    person_n: int

    @property
    def person(self) -> Dict[str, Any]:
        return self.people.directory.get(self.person_n)

    def _spend(self, chat_id: int) -> bool:
        try:
            self.people.directory.spend(self.person)
            return True
        except QuotaExceeded as err:
            self.send(chat_id, f"⏳ {err}")                       # type: ignore[attr-defined]
            return False

    def _handle_command(self, chat_id: int, text: str) -> None:
        cmd, _, arg = text.partition(" ")
        cmd = cmd.split("@", 1)[0].lower()
        person = self.person
        if cmd in {"/start", "/ajuda", "/help"}:
            extra = ("\n\n👥 Você é admin: /pessoas · /convidar <nome> · /remover <n> · /limite <n> <msgs>"
                     if person["papel"] == "admin" else "")
            self.send(chat_id, f"Oi, {person['nome'].split()[0]}! {MEMBER_HELP}{extra}")   # type: ignore[attr-defined]
            return
        if cmd == "/eu":
            self.send(chat_id, self.people.describe_usage(person))         # type: ignore[attr-defined]
            return
        if cmd == "/painel":
            self.send(chat_id, self.people.panel_message(person, from_panel=isinstance(self, AppBridge)))  # type: ignore[attr-defined]
            return
        if cmd in PEOPLE_COMMANDS and person["papel"] == "admin":
            self.send(chat_id, self.people.admin_command(person, cmd, arg.strip()))   # type: ignore[attr-defined]
            return
        if cmd not in MEMBER_COMMANDS:
            self.send(chat_id, "Esse comando é só do dono do agente. Envie /ajuda para ver os seus.")  # type: ignore[attr-defined]
            return
        if cmd not in FREE_COMMANDS and not self._spend(chat_id):
            return
        super()._handle_command(chat_id, text)                             # type: ignore[misc]

    def _handle_chat(self, chat_id: int, text: str) -> None:
        if self._spend(chat_id):
            super()._handle_chat(chat_id, text)                            # type: ignore[misc]

    def _handle_voice(self, chat_id: int, media: Dict[str, Any], private: bool) -> None:
        if self._spend(chat_id):
            super()._handle_voice(chat_id, media, private)                 # type: ignore[misc]

    def _handle_meeting_file(self, chat_id: int, media: Dict[str, Any], caption: str) -> None:
        if self._spend(chat_id):
            super()._handle_meeting_file(chat_id, media, caption)          # type: ignore[misc]


class MemberTelegram(MemberMixin, TelegramBridge):
    """Usa o bot do dono para responder SÓ no chat desta pessoa; nunca faz getUpdates (quem lê é a ponte do dono)."""

    def __init__(self, people: "People", person_n: int, service: Any, owner_bridge: TelegramBridge, chat_id: int):
        TelegramBridge.__init__(self, service, owner_bridge._token, [int(chat_id)], api_base=owner_bridge.api_base)
        self.people, self.person_n = people, person_n

    def start(self) -> None:                                   # nunca disputa as mensagens com a ponte do dono
        return None


class MemberApp(MemberMixin, AppBridge):
    def __init__(self, people: "People", person_n: int, service: Any):
        AppBridge.__init__(self, service)
        self.people, self.person_n = people, person_n


def member_catalog(service: Any, papel: str = "membro") -> List[Dict[str, Any]]:
    from .app_comandos import catalog
    allowed = MEMBER_COMMANDS | (PEOPLE_COMMANDS if papel == "admin" else set())
    out = [c for c in catalog(service) if c["command"].split()[0] in allowed and c["command"] != "/painel"]
    out.append({"group": "Conta", "label": "📊 Meu uso de hoje", "command": "/eu", "ask": "", "example": "", "required": False})
    return out


# ---------------------------------------------------------------- o espaço de cada pessoa
def member_prompt(nome: str) -> str:
    from . import agents, engine
    manifest = engine.ACTIVE_AGENT
    if manifest:
        base = agents.build_agent_prompt(dict(manifest, dono={"usuario": nome}))
    else:
        base = (f"{engine.AGENT_PERSONA}\nVocê atende {nome}.\n\n1. SOBRE O USUÁRIO:\n"
                f"{json.dumps({'usuario': nome}, ensure_ascii=False)}\n\n"
                + engine._PROMPT_TOOLS_SECTION + engine._PROMPT_GUIDELINES_SECTION)
    return base + (f"\nPRIVACIDADE (prioridade máxima): você está no espaço pessoal de {nome}. A agenda, as memórias e os "
                   f"arquivos que você vê são só de {nome}. Você não tem acesso a dados do dono do sistema nem de outras "
                   "pessoas; se pedirem, diga que cada pessoa tem o próprio espaço. Você não envia e-mails nem roda código "
                   "neste espaço.\n")


def build_space(owner: Any, person: Dict[str, Any], root: Path) -> Any:
    """Um serviço completo e separado (banco próprio) para a pessoa, com o modelo e as chaves do dono."""
    from . import agents, engine, local, objetivos, permissoes, relogio, resumos, rotina
    root = Path(root)
    (root / "database").mkdir(parents=True, exist_ok=True)
    vec = engine.VectorEngine()
    owner_vec = owner.db.vector_engine
    if getattr(owner_vec, "remote_embedder", None) is not None:
        vec.remote_embedder, vec.remote_name = owner_vec.remote_embedder, owner_vec.remote_name
    db = engine.UnifiedDatabase(root / "database" / "byte.db", vec, drive_backup_path=root / "database" / "byte_backup.db")
    llm = engine.TrueLLMAutonomousRouter(db=db, ollama_url=getattr(owner.llm, "ollama_url", None))
    llm.mydrive_path = root / "_sem_google_drive"
    for attr in engine.LLM_SETTINGS_CARRIED:
        if hasattr(owner.llm, attr):
            setattr(llm, attr, getattr(owner.llm, attr))
    llm.api_keys = dict(getattr(owner.llm, "api_keys", {}) or {})
    service = engine.ByteEcosystemService(db, llm, engine.RealEmailConnector(db),
                                          engine.MultiFrameGameTerminal(db, root / "workspace"))
    if "web_search" in getattr(owner, "extra_tools", {}):
        engine.install_web_access(service, root)
    if "work_status" in getattr(owner, "extra_actions", {}):
        from .work import WorkAssistant
        WorkAssistant(service, root, {"usuario": person["nome"]}).install()
    relogio.install(service, share=False)
    objetivos.install(service)
    permissoes.install(service)
    resumos.install(service)
    local.install(service)
    rotina.install(service)
    if "meeting_file" in getattr(owner, "extra_actions", {}):
        from . import reunioes
        reunioes.install(service, root)
    manifest = getattr(owner, "agent_manifest", None) or engine.ACTIVE_AGENT
    if manifest:
        agents.apply_to_service(service, manifest)
    seal_space(service, person)
    # o modelo já foi preparado pelo dono (Azure, ou Ollama já ligado): o espaço só herda o estado, sem reinstalar nada
    for attr in ("connection_mode", "ollama_online", "ollama_gpu_warm"):
        if hasattr(owner.llm, attr):
            setattr(llm, attr, getattr(owner.llm, attr))
    return service


def seal_space(service: Any, person: Dict[str, Any]) -> None:
    """Tira do espaço tudo o que é do dono e põe o nome da pessoa no prompt."""
    service.disabled_tools = set(getattr(service, "disabled_tools", set())) | BLOCKED_TOOLS
    for name in BLOCKED_TOOLS:
        service.extra_tools.pop(name, None)
    for name in [a for a in service.extra_actions if a in BLOCKED_ACTIONS or a.startswith("mail_")]:
        service.extra_actions.pop(name, None)
    service.proactive_engine.autonomous_mode_enabled = False       # sem bate-papo espontâneo; lembretes e rotinas seguem
    service.system_prompt = service.llm.system_prompt = member_prompt(person["nome"])
    service.member = {"n": person["n"], "nome": person["nome"]}


class Space:
    def __init__(self, people: "People", person: Dict[str, Any], service: Any):
        self.people, self.n, self.service = people, int(person["n"]), service
        self.app = MemberApp(people, self.n, service)
        self.telegram: Optional[MemberTelegram] = None
        self._queue: "queue.Queue[Tuple[Any, Dict[str, Any]]]" = queue.Queue()
        self._worker: Optional[threading.Thread] = None
        self._calls: List[float] = []

    def telegram_for(self, owner_bridge: TelegramBridge, chat_id: int) -> MemberTelegram:
        tg = self.telegram
        if tg is None or tg._token != owner_bridge._token or int(chat_id) not in tg.allowed:
            tg = self.telegram = MemberTelegram(self.people, self.n, self.service, owner_bridge, chat_id)
        return tg

    def submit(self, bridge: MemberTelegram, upd: Dict[str, Any]) -> None:
        """Cada pessoa tem a própria fila: uma resposta demorada de alguém não segura o bot para os outros."""
        self._queue.put((bridge, upd))
        if self._worker is None or not self._worker.is_alive():
            self._worker = threading.Thread(target=self._drain, daemon=True, name=f"BytePessoa{self.n}")
            self._worker.start()

    def _drain(self) -> None:
        while True:
            try:
                bridge, upd = self._queue.get(timeout=30.0)
            except queue.Empty:
                return
            try:
                bridge.handle_update(upd)
            except Exception as err:
                chat_id = bridge._chat_of(upd)
                try:
                    if chat_id is not None:
                        bridge.send(chat_id, "⚠️ " + re.sub(r"^\w*(Error|Exception): ", "", str(err))[:300])
                except Exception:
                    pass

    def rate_ok(self) -> bool:
        now = time.monotonic()
        self._calls = [t for t in self._calls if now - t < 60.0]
        if len(self._calls) >= PER_MINUTE:
            return False
        self._calls.append(now)
        return True

    def close(self) -> None:
        try:
            self.service.shutdown()
        except Exception:
            pass


# ---------------------------------------------------------------- o gerente
class People:
    def __init__(self, owner: Any, data_dir: Path,
                 builder: Callable[[Any, Dict[str, Any], Path], Any] = build_space):
        self.owner = owner
        self.root = Path(data_dir) / "pessoas"
        self.directory = Directory(self.root / "pessoas.db")
        self.builder = builder
        self._spaces: Dict[int, Space] = {}
        self._lock = threading.RLock()
        self.owner_bridge: Optional[TelegramBridge] = None
        self.bot_username = ""

    # ---- espaços
    def space(self, person: Dict[str, Any]) -> Space:
        n = int(person["n"])
        with self._lock:
            sp = self._spaces.get(n)
            if sp is None:
                sp = self._spaces[n] = Space(self, person, self.builder(self.owner, person, self.root / str(n)))
            return sp

    def load_all(self) -> int:
        """Liga os espaços já existentes (os lembretes de cada pessoa precisam do relógio dela rodando)."""
        count = 0
        for person in self.directory.list():
            if person["chat_id"] is None and not person["painel_hash"]:
                continue                                       # convite ainda não aceito: liga quando a pessoa chegar
            try:
                self.space(person)
                count += 1
            except Exception as err:
                print(f"[PESSOAS] Espaço #{person['n']} não ligou: {type(err).__name__}: {err}", flush=True)
        return count

    def close(self) -> None:
        with self._lock:
            spaces, self._spaces = list(self._spaces.values()), {}
        for sp in spaces:
            sp.close()
        self.directory.close()

    def _drop(self, n: int, wipe: bool = False) -> None:
        with self._lock:
            sp = self._spaces.pop(int(n), None)
        if sp is not None:
            sp.close()
        if wipe:
            target = self.root / str(int(n))
            if target.is_dir() and target.resolve().parent == self.root.resolve():
                shutil.rmtree(target, ignore_errors=True)

    # ---- Telegram
    def route_update(self, owner_bridge: TelegramBridge, chat_id: int, upd: Dict[str, Any]) -> bool:
        """Mensagem de quem NÃO é dono: vai para o espaço da pessoa, ou aceita um convite (/start <código>)."""
        self.owner_bridge = owner_bridge
        person = self.directory.by_chat(chat_id)
        if person is not None:
            sp = self.space(person)
            sp.submit(sp.telegram_for(owner_bridge, chat_id), upd)
            return True
        msg = upd.get("message") or {}
        text = str(msg.get("text") or "").strip()
        m = re.match(r"^/start(?:@\w+)?\s+(\S+)$", text)
        if not m or (msg.get("chat") or {}).get("type", "private") != "private":
            return False
        frm = msg.get("from") or {}
        try:
            person = self.directory.redeem(m.group(1), chat_id, " ".join(filter(None, [frm.get("first_name"), frm.get("last_name")])))
        except PermissionError as err:
            owner_bridge.send(chat_id, f"⛔ {err}")
            return True
        self.space(person)
        from . import engine
        owner_bridge.send(chat_id, f"👋 Oi, {person['nome'].split()[0]}! Eu sou {engine.AGENT_NAME}.\n{MEMBER_HELP}")
        self._tell_owner(f"👥 {person['nome']} entrou pelo convite (Telegram) como {person['papel']}.")
        return True

    def push_all(self, owner_bridge: TelegramBridge) -> int:
        """Entrega os avisos (lembretes, resumos, atas) de cada espaço no Telegram da pessoa."""
        self.owner_bridge = owner_bridge
        sent = 0
        with self._lock:
            spaces = list(self._spaces.values())
        for sp in spaces:
            person = None
            try:
                person = self.directory.get(sp.n)
            except ValueError:
                continue
            if person["chat_id"] is None:
                continue
            try:
                sent += sp.telegram_for(owner_bridge, person["chat_id"]).push_proactive()
            except Exception as err:
                owner_bridge.last_error = f"pessoa #{sp.n}: {type(err).__name__}"
        return sent

    def _bot(self) -> str:
        if not self.bot_username and self.owner_bridge is not None:
            try:
                self.bot_username = str((self.owner_bridge._api("getMe").get("result") or {}).get("username") or "")
            except Exception:
                pass
        return self.bot_username

    def invite_link(self, code: str) -> str:
        bot = self._bot()
        return f"https://t.me/{bot}?start={code}" if bot else f"(Telegram ainda não conectado) código: /start {code}"

    def panel_base(self) -> str:
        from . import engine
        base = os.environ.get("BYTE_LINK_PESSOAS", "").strip().rstrip("/")
        return base or f"/u/{(engine.ACTIVE_AGENT or {}).get('id') or 'matrix'}"

    def panel_link(self, person: Dict[str, Any]) -> str:
        return f"{self.panel_base()}#{self.directory.panel_token(person['n'])}"

    def panel_message(self, person: Dict[str, Any], from_panel: bool = False) -> str:
        if from_panel:
            return "Você já está no seu painel. Para abrir em outro aparelho, peça /painel no Telegram (o link antigo deixa de valer)."
        return ("🖥️ Seu painel pessoal (conversa e comandos em botões):\n" + self.panel_link(person) +
                "\n\nÉ só seu: não compartilhe. Pedir /painel de novo gera outro link e desliga o anterior.")

    def describe_usage(self, person: Dict[str, Any]) -> str:
        used = self.directory.used(person["n"])
        return (f"👤 {person['nome']} ({person['papel']})\n📊 Hoje: {used} de {person['limite']} mensagens "
                "(renova à meia-noite).")

    def _tell_owner(self, text: str) -> None:
        try:
            self.owner.db.enqueue_proactive_message("pessoas", text, brain_used="PESSOAS")
        except Exception:
            pass

    # ---- administração (dono, admins, painel)
    def status(self) -> Dict[str, Any]:
        out = []
        for p in self.directory.list():
            out.append({"n": p["n"], "nome": p["nome"], "papel": p["papel"], "telegram": p["chat_id"] is not None,
                        "telegram_nome": p["telegram_nome"], "painel": bool(p["painel_hash"]),
                        "convite": bool(p["convite_hash"]) and float(p["convite_expira"]) > time.time(),
                        "uso": self.directory.used(p["n"]), "limite": p["limite"], "ligado": p["n"] in self._spaces})
        return {"pessoas": out, "painel_base": self.panel_base(), "limite_padrao": default_limit()}

    def format_list(self) -> str:
        rows = self.status()["pessoas"]
        if not rows:
            return "👥 Ninguém além de você usa este agente.\nConvide alguém: /convidar <nome>  (ou /convidar <nome> admin)"
        lines = ["👥 Pessoas com espaço próprio neste agente:"]
        for p in rows:
            how = []
            if p["telegram"]:
                how.append("Telegram ✅")
            elif p["convite"]:
                how.append("convite pendente ⏳")
            if p["painel"]:
                how.append("painel ✅")
            lines.append(f"{p['n']}. {p['nome']} — {p['papel']} — {', '.join(how) or 'sem acesso ativo'} — "
                         f"hoje {p['uso']}/{p['limite']}")
        lines.append("\n/pessoas painel <n> — link do painel · /pessoas convite <n> — novo convite · "
                     "/limite <n> <msgs> · /remover <n> [apagar]")
        return "\n".join(lines)

    def _invite_text(self, person: Dict[str, Any], code: str) -> str:
        return (f"✉️ Convite para {person['nome']} ({person['papel']}), vale 7 dias e uma única vez:\n"
                f"{self.invite_link(code)}\n\nMande esse link para a pessoa: ela toca, aperta \"Iniciar\" e já cai no "
                f"espaço dela. Para usar só pelo navegador: /pessoas painel {person['n']}")

    def admin_command(self, actor: Optional[Dict[str, Any]], cmd: str, arg: str) -> str:
        """actor=None é o dono. Admins só mexem em membros e não criam outros admins."""
        by = "dono" if actor is None else f"admin #{actor['n']}"
        try:
            if cmd == "/pessoas":
                sub, _, rest = arg.partition(" ")
                if not sub:
                    return self.format_list()
                if sub in {"painel", "convite"}:
                    person = self.directory.get(rest)
                    self._check(actor, person)
                    if sub == "convite":
                        return self._invite_text(person, self.directory.new_invite(person["n"]))
                    return (f"🖥️ Painel pessoal de {person['nome']} (o anterior, se havia, deixou de valer):\n"
                            f"{self.panel_link(person)}\n\nEnvie só para a pessoa: quem tem o link entra no espaço dela.")
                return "Use: /pessoas · /pessoas painel <n> · /pessoas convite <n>"
            if cmd == "/convidar":
                words = arg.split()
                papel = "membro"
                if words and words[-1].lower() in ROLES:
                    papel = words.pop().lower()
                if papel == "admin" and actor is not None:
                    return "Só o dono cria administradores."
                person, code = self.directory.add(" ".join(words), papel, by)
                return self._invite_text(person, code)
            if cmd == "/remover":
                parts = arg.split()
                if not parts:
                    return "Use: /remover <n> (mantém os dados) ou /remover <n> apagar (apaga tudo da pessoa)"
                person = self.directory.get(parts[0])
                self._check(actor, person)
                wipe = len(parts) > 1 and parts[1].lower() in {"apagar", "tudo"}
                self.directory.remove(person["n"])
                self._drop(person["n"], wipe=wipe)
                return (f"🗑️ {person['nome']} não tem mais acesso (Telegram e painel desligados)."
                        + (" Os dados dela foram apagados." if wipe else " Os dados ficam guardados; /remover só tira o acesso."))
            if cmd == "/limite":
                parts = arg.split()
                if len(parts) != 2:
                    return f"Use: /limite <n> <mensagens por dia> (padrão {default_limit()})"
                person = self.directory.get(parts[0])
                self._check(actor, person)
                person = self.directory.set_limit(person["n"], parts[1])
                return f"✅ {person['nome']}: até {person['limite']} mensagens por dia."
        except (ValueError, PermissionError) as err:
            return f"⚠️ {err}"
        return "Comando desconhecido."

    @staticmethod
    def _check(actor: Optional[Dict[str, Any]], person: Dict[str, Any]) -> None:
        if actor is not None and (person["papel"] == "admin" and person["n"] != actor["n"]):
            raise PermissionError("Admins só gerenciam membros.")

    # ---- painel pessoal (/u/<agente>)
    def route(self, h: Any, method: str, path: str, query: str) -> None:
        parts = path.strip("/").split("/")                     # ["u", "<agente>", "api", "..."]
        sub = "/".join(parts[2:])
        if not sub:
            if method != "GET":
                h._send_json(405, {"error": "Use GET"})
                return
            from .pessoa_pagina import page
            from . import engine
            h._send_html(page(engine.AGENT_NAME))
            return
        if not sub.startswith("api/"):
            h._send_json(404, {"error": "Página não encontrada"})
            return
        person = self.directory.by_panel(h._bearer())
        if person is None:
            h._send_json(401, {"error": "Este link de painel não vale mais. Peça um novo com /painel no Telegram "
                                       "ou para quem te convidou."})
            return
        sp = self.space(person)
        if not sp.rate_ok():
            h._send_json(429, {"error": "Muitos pedidos por minuto; espere um pouco."})
            return
        from . import engine
        if sub == "api/eu" and method == "GET":
            h._send_json(200, {"nome": person["nome"], "papel": person["papel"], "agente": engine.AGENT_NAME,
                               "uso": self.directory.used(person["n"]), "limite": person["limite"]})
        elif sub == "api/commands" and method == "GET":
            h._send_json(200, {"commands": member_catalog(sp.service, person["papel"])})
        elif sub == "api/command" and method == "POST":
            body = h._read_json()
            if body is None:
                h._send_json(400, {"error": 'Envie JSON {"text": "..."}'})
                return
            msgs = sp.app.run(str(body.get("text") or "").strip()[:2000], str(body.get("callback") or "")[:80])
            h._send_json(200, {"messages": msgs})
        else:
            h._send_json(404, {"error": "Rota desconhecida"})


def install(service: Any, data_dir: Path, autoload: bool = True) -> People:
    people = People(service, data_dir)
    service.people = people
    service.public_routes = dict(getattr(service, "public_routes", None) or {}, **{"/u/": people.route})
    service.status_providers["pessoas"] = people.status
    service.extra_actions["people_command"] = lambda p: {
        "text": people.admin_command(None, str(p.get("cmd") or ""), str(p.get("arg") or "").strip()),
        "pessoas": people.status()}
    original_shutdown = service.shutdown

    def shutdown() -> None:
        try:
            people.close()
        finally:
            original_shutdown()
    service.shutdown = shutdown
    if autoload and people.directory.list():
        threading.Thread(target=people.load_all, daemon=True, name="BytePessoas").start()
    return people

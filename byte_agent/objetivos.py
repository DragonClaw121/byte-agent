"""Objetivos: tarefas longas que a Byte executa sozinha, em etapas, ao longo de horas ou dias — v13.9.

Você diz "prepare um resumo das notícias de IA da semana até sexta" e ela:
  1. planeja: divide o pedido em 2 a 6 etapas (a última sempre entrega o resultado final);
  2. executa UMA etapa por vez, em segundo plano, com as ferramentas de tarefa desassistida (leitura, busca, agenda,
     memória; nada de e-mail, código ou publicação sem você), levando o resultado das etapas anteriores adiante;
  3. confere cada resultado: vazio ou "não consegui" vira nova tentativa; depois de 2 falhas o objetivo fica
     BLOQUEADO e ela pede a sua ajuda;
  4. avisa no painel e no Telegram quando termina (com o resultado) ou quando trava.

Limites de segurança: no máximo MAX_ACTIVE objetivos ativos, MAX_STEPS etapas cada, uma etapa por vez em todo o
sistema, intervalo mínimo entre etapas e o orçamento diário de tokens (BYTE_ORCAMENTO_TOKENS_DIA): estourou, a
autonomia espera o dia seguinte. Pedidos que você faz diretamente nunca são bloqueados pelo orçamento.
"""
from __future__ import annotations

import os
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

MAX_ACTIVE = 5
MAX_STEPS = 6
MAX_ATTEMPTS = 2
STEP_SPACING_S = 60.0            # pausa entre etapas do mesmo objetivo
RETRY_SPACING_S = 600.0          # espera antes de tentar de novo uma etapa que falhou
CARRY_CHARS = 1800               # quanto do resultado anterior segue para a próxima etapa
DEFAULT_DAILY_BUDGET = 400_000   # tokens por dia para trabalho autônomo (≈ US$ 0,20 com um modelo mini)

_FAIL_RE = re.compile(r"(não consegui|nao consegui|não foi possível|nao foi possivel|erro:|error:|indisponível|"
                      r"sem acesso|não tenho acesso|i can't|i cannot|unable to)", re.I)
_STEP_LINE_RE = re.compile(r"^\s*(?:\d{1,2}\s*[.)\-:]|[-*•])\s*(.+?)\s*$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS goals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',
    deadline_epoch REAL,
    next_run_epoch REAL NOT NULL DEFAULT 0,
    result TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS goal_steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    goal_id INTEGER NOT NULL,
    idx INTEGER NOT NULL,
    text TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    result TEXT NOT NULL DEFAULT '',
    attempts INTEGER NOT NULL DEFAULT 0,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_goal_steps_goal ON goal_steps(goal_id, idx);
"""

STATUS_LABEL = {"active": "em andamento", "blocked": "bloqueado", "done": "concluído", "cancelled": "cancelado",
                "paused": "pausado"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean(text: Any, limit: int) -> str:
    return " ".join(str(text or "").split())[:limit]


def parse_steps(text: str, goal_title: str) -> List[str]:
    """Lista numerada/marcada do planejador -> etapas. Garante a etapa final de entrega."""
    steps: List[str] = []
    for line in str(text or "").splitlines():
        m = _STEP_LINE_RE.match(line)
        if m:
            step = _clean(m.group(1).strip("*_ "), 300)
            if len(step) >= 4 and step not in steps:
                steps.append(step)
    if not steps:
        steps = [_clean(goal_title, 300)]
    steps = steps[:MAX_STEPS - 1] if len(steps) >= MAX_STEPS else steps
    last = steps[-1].lower()
    if not any(w in last for w in ("entreg", "resultado final", "resum", "apresent", "envi", "consolid")):
        steps.append("Revisar o que as etapas anteriores produziram e entregar o resultado final ao usuário, completo e organizado.")
    return steps[:MAX_STEPS]


class GoalStore:
    """Persistência dos objetivos no mesmo banco SQLite da Byte (entra no backup e no snapshot da nuvem)."""

    def __init__(self, db: Any):
        self.db = db
        with db.connection() as conn:
            conn.executescript(SCHEMA)

    def create(self, title: str, steps: List[str], detail: str = "", deadline_epoch: Optional[float] = None) -> Dict[str, Any]:
        title = _clean(title, 200)
        if len(title) < 3:
            raise ValueError("Descreva o objetivo.")
        if not steps:
            raise ValueError("O objetivo precisa de ao menos uma etapa.")
        now = _now_iso()
        with self.db.connection() as conn:
            active = conn.execute("SELECT COUNT(*) AS n FROM goals WHERE status IN ('active', 'blocked')").fetchone()["n"]
            if active >= MAX_ACTIVE:
                raise ValueError(f"Já há {MAX_ACTIVE} objetivos em andamento; conclua ou cancele um antes.")
            cur = conn.execute(
                "INSERT INTO goals (title, detail, status, deadline_epoch, next_run_epoch, created_at, updated_at) "
                "VALUES (?, ?, 'active', ?, 0, ?, ?)", (title, _clean(detail, 1500), deadline_epoch, now, now))
            gid = cur.lastrowid
            for i, step in enumerate(steps[:MAX_STEPS]):
                conn.execute("INSERT INTO goal_steps (goal_id, idx, text) VALUES (?, ?, ?)", (gid, i, _clean(step, 300)))
        self.db.record_audit("goal_created", "objetivos", "success", {"id": gid, "steps": len(steps)})
        return self.get(gid)

    def get(self, goal_id: int) -> Optional[Dict[str, Any]]:
        with self.db.connection() as conn:
            row = conn.execute("SELECT * FROM goals WHERE id = ?", (int(goal_id),)).fetchone()
            if row is None:
                return None
            steps = conn.execute("SELECT * FROM goal_steps WHERE goal_id = ? ORDER BY idx", (int(goal_id),)).fetchall()
        goal = dict(row)
        goal["steps"] = [dict(s) for s in steps]
        goal["done_steps"] = sum(1 for s in goal["steps"] if s["status"] == "done")
        goal["status_label"] = STATUS_LABEL.get(goal["status"], goal["status"])
        return goal

    def list(self, include_finished: bool = False) -> List[Dict[str, Any]]:
        query = "SELECT id FROM goals" + ("" if include_finished else " WHERE status IN ('active', 'blocked', 'paused')") + \
                " ORDER BY id DESC LIMIT 30"
        with self.db.connection() as conn:
            ids = [r["id"] for r in conn.execute(query).fetchall()]
        return [g for g in (self.get(i) for i in ids) if g]

    def set_status(self, goal_id: int, status: str, result: Optional[str] = None) -> bool:
        if status not in STATUS_LABEL:
            raise ValueError("status inválido")
        with self.db.connection() as conn:
            if result is None:
                cur = conn.execute("UPDATE goals SET status = ?, updated_at = ? WHERE id = ?", (status, _now_iso(), int(goal_id)))
            else:
                cur = conn.execute("UPDATE goals SET status = ?, result = ?, updated_at = ? WHERE id = ?",
                                   (status, str(result)[:20000], _now_iso(), int(goal_id)))
            return cur.rowcount > 0

    def resume(self, goal_id: int) -> bool:
        """Bloqueado/pausado -> ativo de novo; a etapa que falhou volta a ter tentativas."""
        with self.db.connection() as conn:
            cur = conn.execute("UPDATE goals SET status = 'active', next_run_epoch = 0, updated_at = ? "
                               "WHERE id = ? AND status IN ('blocked', 'paused')", (_now_iso(), int(goal_id)))
            if cur.rowcount:
                conn.execute("UPDATE goal_steps SET status = 'pending', attempts = 0 WHERE goal_id = ? AND status = 'failed'",
                             (int(goal_id),))
            return cur.rowcount > 0

    def next_due(self, now: float) -> Optional[Dict[str, Any]]:
        with self.db.connection() as conn:
            row = conn.execute("SELECT id FROM goals WHERE status = 'active' AND next_run_epoch <= ? "
                               "ORDER BY COALESCE(deadline_epoch, 9e18), id LIMIT 1", (now,)).fetchone()
        return self.get(row["id"]) if row else None

    def record_step(self, step_id: int, status: str, result: str, attempts: int) -> None:
        with self.db.connection() as conn:
            conn.execute("UPDATE goal_steps SET status = ?, result = ?, attempts = ?, finished_at = ? WHERE id = ?",
                         (status, str(result)[:12000], attempts, _now_iso() if status in {"done", "failed"} else None, step_id))

    def schedule(self, goal_id: int, when: float) -> None:
        with self.db.connection() as conn:
            conn.execute("UPDATE goals SET next_run_epoch = ?, updated_at = ? WHERE id = ?", (when, _now_iso(), int(goal_id)))


class GoalRunner:
    """Executa os objetivos. `planner(prompt) -> texto` e `executor(instrução) -> texto` são do serviço (LLM com as
    ferramentas de tarefa desassistida); `notify(texto)` entrega no painel e no Telegram."""

    def __init__(self, store: GoalStore, planner: Callable[[str], str], executor: Callable[[str], str],
                 notify: Callable[[str], None], tokens_today: Optional[Callable[[], int]] = None,
                 daily_budget: Optional[int] = None, clock: Callable[[], float] = time.time):
        self.store, self.planner, self.executor, self.notify = store, planner, executor, notify
        self.tokens_today = tokens_today or (lambda: 0)
        if daily_budget is None:
            try:
                daily_budget = int(os.environ.get("BYTE_ORCAMENTO_TOKENS_DIA", "") or DEFAULT_DAILY_BUDGET)
            except ValueError:
                daily_budget = DEFAULT_DAILY_BUDGET
        self.daily_budget = max(0, daily_budget)
        self.clock = clock
        self._busy = threading.Lock()
        self.last_error: Optional[str] = None
        self._budget_warned_day: Optional[str] = None

    # ---------------------------------------------------------------- orçamento
    def budget_left(self) -> Optional[int]:
        if not self.daily_budget:
            return None                                           # 0 = sem limite
        return self.daily_budget - int(self.tokens_today() or 0)

    def budget_ok(self) -> bool:
        left = self.budget_left()
        return left is None or left > 0

    # ---------------------------------------------------------------- criar
    def plan(self, title: str, detail: str = "") -> List[str]:
        prompt = ("Você vai trabalhar sozinho, em segundo plano, neste objetivo do usuário:\n"
                  f"OBJETIVO: {title}\n" + (f"DETALHES: {detail}\n" if detail else "") +
                  f"Divida em 2 a {MAX_STEPS} etapas concretas que você consegue executar com busca na web, leitura de "
                  "páginas, agenda, tarefas e memória (sem enviar e-mails, rodar código ou publicar nada). Cada etapa em "
                  "uma linha, numerada (1., 2., ...), começando por um verbo. A última etapa entrega o resultado final ao "
                  "usuário. Responda SOMENTE com a lista.")
        try:
            return parse_steps(self.planner(prompt), title)
        except Exception as err:                                   # sem planejador, uma etapa só (o próprio pedido)
            self.last_error = f"planejamento: {type(err).__name__}: {str(err)[:120]}"
            return parse_steps("", title)

    def create(self, title: str, detail: str = "", deadline_epoch: Optional[float] = None) -> Dict[str, Any]:
        steps = self.plan(title, detail)
        return self.store.create(title, steps, detail, deadline_epoch)

    # ---------------------------------------------------------------- executar
    def tick(self) -> Optional[Dict[str, Any]]:
        """Executa no máximo UMA etapa (a mais urgente). Devolve um resumo do que fez, ou None."""
        if not self._busy.acquire(blocking=False):
            return None
        try:
            now = self.clock()
            goal = self.store.next_due(now)
            if goal is None:
                return None
            if not self.budget_ok():
                day = datetime.now().strftime("%Y-%m-%d")
                if self._budget_warned_day != day:
                    self._budget_warned_day = day
                    self.notify(f"💸 Orçamento diário de tokens da autonomia atingido ({self.daily_budget:,}). "
                                "Os objetivos continuam amanhã; pedidos diretos seguem normais.".replace(",", "."))
                return {"goal": goal["id"], "skipped": "budget"}
            step = next((s for s in goal["steps"] if s["status"] in {"pending", "running", "failed"}), None)
            if step is None:
                return self._finish(goal)
            return self._run_step(goal, step, now)
        finally:
            self._busy.release()

    def _instruction(self, goal: Dict[str, Any], step: Dict[str, Any]) -> str:
        done = [s for s in goal["steps"] if s["status"] == "done"]
        lines = [f"OBJETIVO DE LONGO PRAZO #{goal['id']}: {goal['title']}"]
        if goal.get("detail"):
            lines.append(f"Detalhes do usuário: {goal['detail']}")
        if goal.get("deadline_epoch"):
            lines.append("Prazo: " + datetime.fromtimestamp(goal["deadline_epoch"]).strftime("%d/%m/%Y %H:%M"))
        lines.append(f"Etapas: {len(goal['steps'])}. Você está na etapa {step['idx'] + 1}.")
        if done:
            lines.append("O que as etapas anteriores já produziram:")
            budget = CARRY_CHARS * 2
            for s in done[-3:]:
                chunk = s["result"][:CARRY_CHARS]
                budget -= len(chunk)
                lines.append(f"- Etapa {s['idx'] + 1} ({s['text']}): {chunk}")
                if budget <= 0:
                    break
        lines.append(f"FAÇA AGORA SÓ ESTA ETAPA: {step['text']}")
        lines.append("Entregue o resultado concreto desta etapa (dados, texto, lista, links). Se for impossível com as "
                     "ferramentas disponíveis, comece a resposta com 'NÃO CONSEGUI:' e explique o que falta.")
        return "\n".join(lines)

    def _run_step(self, goal: Dict[str, Any], step: Dict[str, Any], now: float) -> Dict[str, Any]:
        attempts = int(step["attempts"]) + 1
        self.store.record_step(step["id"], "running", step["result"], attempts)
        try:
            result = str(self.executor(self._instruction(goal, step)) or "").strip()
            error = None
        except Exception as err:
            result, error = "", f"{type(err).__name__}: {str(err)[:200]}"
        ok = bool(result) and len(result) >= 20 and not _FAIL_RE.search(result[:200])
        if ok:
            self.store.record_step(step["id"], "done", result, attempts)
            self.store.schedule(goal["id"], now + STEP_SPACING_S)
            return {"goal": goal["id"], "step": step["idx"] + 1, "status": "done"}
        reason = error or (result[:300] if result else "a etapa não produziu resultado")
        if attempts < MAX_ATTEMPTS:
            self.store.record_step(step["id"], "failed", reason, attempts)
            self.store.schedule(goal["id"], now + RETRY_SPACING_S)
            return {"goal": goal["id"], "step": step["idx"] + 1, "status": "retry"}
        self.store.record_step(step["id"], "failed", reason, attempts)
        self.store.set_status(goal["id"], "blocked")
        self.notify(f"🧩 Objetivo #{goal['id']} travou na etapa {step['idx'] + 1} de {len(goal['steps'])}: "
                    f"{step['text']}\nMotivo: {reason[:400]}\n\nMe diga como seguir e depois use /retomar {goal['id']} "
                    f"(ou /cancelar_objetivo {goal['id']}).")
        return {"goal": goal["id"], "step": step["idx"] + 1, "status": "blocked"}

    def _finish(self, goal: Dict[str, Any]) -> Dict[str, Any]:
        final = goal["steps"][-1]["result"] if goal["steps"] else ""
        self.store.set_status(goal["id"], "done", final)
        self.store.db.record_audit("goal_done", "objetivos", "success", {"id": goal["id"]})
        self.notify(f"✅ Objetivo #{goal['id']} concluído: {goal['title']}\n\n{final[:3500]}")
        return {"goal": goal["id"], "status": "finished"}

    # ---------------------------------------------------------------- para o painel e o Telegram
    def summary(self, include_finished: bool = False) -> Dict[str, Any]:
        goals = self.store.list(include_finished)
        return {"goals": [{"id": g["id"], "title": g["title"], "status": g["status"], "status_label": g["status_label"],
                           "progress": f"{g['done_steps']}/{len(g['steps'])}",
                           "steps": [{"text": s["text"], "status": s["status"]} for s in g["steps"]]} for g in goals],
                "budget": {"daily": self.daily_budget or None, "left": self.budget_left()},
                "last_error": self.last_error}


def format_goals(summary: Dict[str, Any]) -> str:
    if not summary.get("goals"):
        return "Nenhum objetivo em andamento. Crie um com /objetivo <o que você quer>."
    icons = {"done": "✅", "running": "⏳", "failed": "⚠️", "pending": "▫️"}
    out = []
    for g in summary["goals"]:
        out.append(f"#{g['id']} {g['title']} — {g['status_label']} ({g['progress']})")
        out += [f"   {icons.get(s['status'], '▫️')} {s['text']}" for s in g["steps"]]
    left = (summary.get("budget") or {}).get("left")
    if left is not None:
        out.append(f"\nOrçamento de hoje para a autonomia: {max(0, left):,} tokens".replace(",", "."))
    return "\n".join(out)


# ---------------------------------------------------------------- ligação com o serviço da Byte
_GOAL_INTENT_RE = re.compile(r"\b(objetivos?|metas?|em etapas|ao longo d|trabalh\w* nisso|projeto longo|"
                             r"ate (segunda|terca|quarta|quinta|sexta|sabado|domingo|amanha|o fim|semana que vem))\b")
_DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{1,2}):(\d{2}))?")


def parse_deadline(value: Any) -> Optional[float]:
    m = _DATE_RE.search(str(value or ""))
    if not m:
        return None
    y, mo, d, h, mi = m.groups()
    try:
        return datetime(int(y), int(mo), int(d), int(h or 18), int(mi or 0)).timestamp()
    except ValueError:
        return None


def install(service: Any, tokens_today: Optional[Callable[[], int]] = None) -> GoalRunner:
    """Cria as tabelas, o executor e as entradas: ferramenta do chat, ações do painel/Telegram e o relógio."""
    from . import engine
    if tokens_today is None:
        from .cloud import tokens_today as _tt
        tokens_today = _tt
    db = service.db

    def notify(text: str) -> None:
        db.enqueue_proactive_message("goal", text, brain_used="OBJETIVOS")

    runner = GoalRunner(GoalStore(db), planner=service.run_scheduled_mission, executor=service.run_scheduled_mission,
                        notify=notify, tokens_today=tokens_today)
    service.goal_runner = runner
    state = {"thread": None}

    def tick_in_background() -> None:
        llm = service.llm
        if not llm.llm_ready():
            return
        th = state["thread"]
        if th is not None and th.is_alive():
            return
        th = threading.Thread(target=_safe_tick, daemon=True, name="ByteObjetivos")
        state["thread"] = th
        th.start()

    def _safe_tick() -> None:
        try:
            runner.tick()
        except Exception as err:                          # o relógio nunca pode cair por causa de um objetivo
            runner.last_error = f"{type(err).__name__}: {str(err)[:160]}"

    sched = getattr(service, "scheduler", None)
    if sched is not None:                               # v13.34: relógio central (intervalo, falhas, /saude)
        sched.add("objetivos", 10, runner.tick, when=lambda: service.llm.llm_ready(), label="Objetivos")
    else:
        service.proactive_engine.background_jobs.append(tick_in_background)
    service.proactive_engine.budget_ok = runner.budget_ok

    def tool_create(args: Dict[str, Any], turn: Dict[str, Any]) -> str:
        if turn.get("tainted"):
            return ("BLOQUEADO: este pedido leu conteúdo da internet, então não crio objetivos automaticamente nele. "
                    "Peça ao usuário que confirme numa nova mensagem.")
        title = _clean(args.get("titulo") or args.get("title"), 200)
        goal = runner.create(title, str(args.get("detalhes") or ""), parse_deadline(args.get("prazo")))
        steps = "\n".join(f"{i + 1}. {s['text']}" for i, s in enumerate(goal["steps"]))
        turn.setdefault("blocks", []).append(f"🧩 Objetivo #{goal['id']} criado: {goal['title']}\n{steps}")
        return (f"Objetivo #{goal['id']} criado com {len(goal['steps'])} etapas (o plano já foi mostrado ao usuário). "
                "Diga em uma frase que você vai trabalhar nisso em segundo plano e avisará ao terminar.")

    def tool_list(args: Dict[str, Any], turn: Dict[str, Any]) -> str:
        return format_goals(runner.summary())

    service.extra_tools.update({
        "criar_objetivo": {
            "auto": True, "mission": False, "handler": tool_create, "intent": _GOAL_INTENT_RE,
            "description": ("Cria um OBJETIVO de longo prazo que você executa sozinho, em etapas, em segundo plano (ex.: "
                            "'pesquise X e me entregue um resumo até sexta'). Use quando o pedido for trabalhoso ou tiver prazo."),
            "parameters": {"type": "object", "required": ["titulo"], "properties": {
                "titulo": {"type": "string", "description": "O objetivo, em uma frase"},
                "detalhes": {"type": "string", "description": "Preferências e critérios do usuário"},
                "prazo": {"type": "string", "description": "Data limite AAAA-MM-DD [HH:MM], se houver"}}}},
        "listar_objetivos": {
            "auto": True, "mission": True, "handler": tool_list, "intent": _GOAL_INTENT_RE,
            "description": "Lista os objetivos de longo prazo e o progresso de cada etapa.",
            "parameters": {"type": "object", "properties": {}}},
    })

    def act_create(payload: Dict[str, Any]) -> Dict[str, Any]:
        goal = runner.create(str(payload.get("title") or ""), str(payload.get("detail") or ""),
                             parse_deadline(payload.get("deadline")))
        steps = "\n".join(f"{i + 1}. {st['text']}" for i, st in enumerate(goal["steps"]))
        return {"goal_id": goal["id"], "steps": len(goal["steps"]),
                "text": f"🧩 Objetivo #{goal['id']} criado: {goal['title']}\n{steps}\n\n"
                        "Vou trabalhar nisso em segundo plano, uma etapa por vez, e aviso quando terminar."}

    def act_cancel(payload: Dict[str, Any]) -> Dict[str, Any]:
        return {"cancelled": runner.store.set_status(int(payload.get("goal_id") or 0), "cancelled")}

    def act_resume(payload: Dict[str, Any]) -> Dict[str, Any]:
        return {"resumed": runner.store.resume(int(payload.get("goal_id") or 0))}

    service.extra_actions.update({
        "goal_create": act_create,
        "goal_list": lambda payload: dict(runner.summary(bool(payload.get("all"))),
                                          text=format_goals(runner.summary(bool(payload.get("all"))))),
        "goal_cancel": act_cancel,
        "goal_resume": act_resume,
        "goal_tick": lambda payload: {"ran": runner.tick()},
    })
    service.status_providers["goals"] = lambda: runner.summary()
    return runner

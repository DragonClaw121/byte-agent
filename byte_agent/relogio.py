"""Relógio central dos agentes e /saude — v13.34 (R0.5).

Antes, cada módulo (objetivos, gatilhos, resumos, rotina, memória) punha a sua função no laço de 8 s e controlava
sozinho o intervalo, a thread e os erros. Erro de um vigia ficava escondido até alguém olhar /gatilhos.

Agora:
  service.scheduler.add(nome, intervalo_s, função, quando=..., erro=...)
  - cada tarefa roda no seu intervalo, numa thread própria, nunca duas vezes ao mesmo tempo;
  - exceção ou erro informado (erro=lambda: ...) conta como falha; 3 falhas seguidas => aviso no Telegram/app
    ("⚠️ ... está falhando"), e outro aviso quando volta a funcionar;
  - /saude mostra tudo: tarefas, últimos erros, versão, memória, tokens do dia, aprovações pendentes.

Também aqui: cache curto compartilhado para leituras repetidas (agenda iCal, clima), que antes eram baixadas várias
vezes por hora por módulos diferentes.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

ALERT_AFTER = 3


# ---------------------------------------------------------------- cache curto compartilhado
class TTLCache:
    def __init__(self, max_items: int = 256):
        self._data: Dict[str, Tuple[float, Any]] = {}
        self._lock = threading.Lock()
        self._inflight: Dict[str, threading.Lock] = {}
        self.max_items = max_items
        self.hits = self.misses = 0

    def get_or_fetch(self, key: str, ttl_s: float, fetch: Callable[[], Any]) -> Any:
        now = time.time()
        with self._lock:
            item = self._data.get(key)
            if item and now - item[0] < ttl_s:
                self.hits += 1
                return item[1]
            gate = self._inflight.setdefault(key, threading.Lock())
        with gate:                                    # dois pedidos iguais ao mesmo tempo: só um vai à internet
            with self._lock:
                item = self._data.get(key)
                if item and time.time() - item[0] < ttl_s:
                    self.hits += 1
                    return item[1]
            self.misses += 1
            value = fetch()                           # erro não entra no cache
            with self._lock:
                self._data[key] = (time.time(), value)
                if len(self._data) > self.max_items:
                    oldest = sorted(self._data.items(), key=lambda kv: kv[1][0])[: len(self._data) - self.max_items]
                    for k, _ in oldest:
                        self._data.pop(k, None)
            return value

    def clear(self) -> None:
        with self._lock:
            self._data.clear()


SHARED = TTLCache()


def cached(key: str, ttl_s: float, fetch: Callable[[], Any]) -> Any:
    return SHARED.get_or_fetch(key, ttl_s, fetch)


# ---------------------------------------------------------------- relógio
@dataclass
class Job:
    name: str
    interval_s: float
    fn: Callable[[], Any]
    when: Optional[Callable[[], bool]] = None
    error_of: Optional[Callable[[], Optional[str]]] = None
    label: str = ""
    last_start: float = 0.0
    last_end: float = 0.0
    last_ok: float = 0.0
    last_error: Optional[str] = None
    failures: int = 0
    runs: int = 0
    alerted: bool = False
    thread: Optional[threading.Thread] = field(default=None, repr=False)


class Scheduler:
    def __init__(self, notify: Callable[[str], None], clock: Callable[[], float] = time.time):
        self.notify, self.clock = notify, clock
        self.jobs: Dict[str, Job] = {}
        self.started = clock()
        self._lock = threading.Lock()

    def add(self, name: str, interval_s: float, fn: Callable[[], Any], when: Optional[Callable[[], bool]] = None,
            error_of: Optional[Callable[[], Optional[str]]] = None, label: str = "") -> Job:
        job = Job(name, float(interval_s), fn, when, error_of, label or name)
        with self._lock:
            self.jobs[name] = job
        return job

    def tick(self, wait: bool = False) -> List[str]:
        """Chamado pelo laço da autonomia (a cada 8 s): dispara o que está na hora. wait=True (testes) roda junto."""
        started: List[str] = []
        now = self.clock()
        for job in list(self.jobs.values()):
            if job.thread is not None and job.thread.is_alive():
                continue
            if now - job.last_start < job.interval_s:
                continue
            try:
                if job.when is not None and not job.when():
                    continue
            except Exception:
                continue
            job.last_start = now
            if wait:
                self._run(job)
            else:
                job.thread = threading.Thread(target=self._run, args=(job,), daemon=True, name=f"Relogio-{job.name}")
                job.thread.start()
            started.append(job.name)
        return started

    def _run(self, job: Job) -> None:
        error: Optional[str] = None
        try:
            job.fn()
            if job.error_of is not None:
                error = job.error_of() or None
        except Exception as err:
            error = f"{type(err).__name__}: {str(err)[:200]}"
        job.runs += 1
        job.last_end = self.clock()
        if error:
            job.failures += 1
            job.last_error = str(error)[:300]
            if job.failures >= ALERT_AFTER and not job.alerted:
                job.alerted = True
                self.notify(f"⚠️ {job.label} está falhando ({job.failures} vezes seguidas): {job.last_error}\n"
                            "Detalhes em /saude.")
        else:
            if job.alerted:
                self.notify(f"✅ {job.label} voltou a funcionar.")
            job.failures, job.alerted, job.last_ok = 0, False, job.last_end
            job.last_error = None

    def status(self) -> List[Dict[str, Any]]:
        now = self.clock()
        return [{"name": j.name, "label": j.label, "interval_s": j.interval_s, "runs": j.runs, "failures": j.failures,
                 "last_error": j.last_error, "last_run_s": int(now - j.last_end) if j.last_end else None,
                 "running": bool(j.thread is not None and j.thread.is_alive())} for j in self.jobs.values()]


def _ago(seconds: Optional[int]) -> str:
    if seconds is None:
        return "ainda não rodou"
    if seconds < 90:
        return f"há {seconds} s"
    if seconds < 5400:
        return f"há {seconds // 60} min"
    return f"há {seconds // 3600} h"


def health(service: Any) -> Dict[str, Any]:
    from . import engine
    sched = getattr(service, "scheduler", None)
    jobs = sched.status() if sched else []
    try:
        from .cloud import tokens_today
        tokens = tokens_today()
    except Exception:
        tokens = None
    runner = getattr(service, "goal_runner", None)
    budget = getattr(runner, "daily_budget", None) if runner else None
    try:
        pending = len(service.pending_actions)
    except Exception:
        pending = None
    try:
        size_kb = int(service.db.db_path.stat().st_size // 1024)
    except Exception:
        size_kb = None
    llm = service.llm
    out = {
        "agent": engine.AGENT_NAME, "version": engine.BYTE_VERSION,
        "uptime_s": int(time.time() - (sched.started if sched else time.time())),
        "llm": {"ready": bool(llm.llm_ready()), "model": f"{getattr(llm, 'active_provider', '?')}:{getattr(llm, 'active_model', '?')}"},
        "memory_engine": service.db.vector_engine.engine_id(), "tokens_today": tokens, "token_budget": budget,
        "pending_approvals": pending, "db_kb": size_kb, "jobs": jobs,
        "cache": {"hits": SHARED.hits, "misses": SHARED.misses},
    }
    out["ok"] = out["llm"]["ready"] and not any(j["failures"] >= ALERT_AFTER for j in jobs)
    return out


def format_health(h: Dict[str, Any]) -> str:
    up = h["uptime_s"]
    lines = [f"{'🟢' if h['ok'] else '🟠'} {h['agent']} v{h['version']} · no ar há {up // 3600} h {up % 3600 // 60} min",
             f"🧠 Modelo: {h['llm']['model']} ({'pronto' if h['llm']['ready'] else 'FORA DO AR'})",
             f"🔎 Memória: {'por significado (Azure)' if 'azure' in h['memory_engine'] else 'por palavras' if h['memory_engine'] == 'hash-v1' else h['memory_engine']}"]
    if h["tokens_today"] is not None:
        lines.append(f"💸 Tokens hoje: {h['tokens_today']:,}".replace(",", ".")
                     + (f" de {h['token_budget']:,} da autonomia".replace(",", ".") if h.get("token_budget") else ""))
    if h["pending_approvals"]:
        lines.append(f"🔐 Aprovações esperando você: {h['pending_approvals']}")
    if h["jobs"]:
        lines.append("⏱️ Tarefas de fundo:")
        for j in h["jobs"]:
            icon = "⚠️" if j["failures"] >= ALERT_AFTER else ("🟡" if j["failures"] else "✅")
            lines.append(f"{icon} {j['label']}: {_ago(j['last_run_s'])}"
                         + (f" — {j['failures']} falha(s): {j['last_error']}" if j["failures"] else ""))
    return "\n".join(lines)


def install(service: Any) -> Scheduler:
    sched = Scheduler(lambda text: service.db.enqueue_proactive_message("saude", text, brain_used="SAÚDE"))
    service.scheduler = sched
    service.proactive_engine.background_jobs.append(sched.tick)
    service.extra_actions["health"] = lambda payload: dict(health(service), text=format_health(health(service)))
    service.status_providers["saude"] = lambda: {"ok": health(service)["ok"], "jobs": sched.status()}
    return sched

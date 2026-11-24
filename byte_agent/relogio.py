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


MEMORY_WARN = 0.80                       # v13.57: acima disso o /saude avisa; acima de 90% o servidor arrisca reiniciar


def _read_int(path: str) -> Optional[int]:
    try:
        with open(path) as fh:
            raw = fh.read().strip()
        return None if raw in ("", "max") else int(raw)
    except (OSError, ValueError):
        return None


def memory_usage(cgroup_root: str = "/sys/fs/cgroup", proc_status: str = "/proc/self/status") -> Dict[str, Any]:
    """Memória do contêiner inteiro (cgroup v2 ou v1) e deste processo (RSS), em MB. Fora de contêiner: só o processo."""
    used = _read_int(f"{cgroup_root}/memory.current")
    limit = _read_int(f"{cgroup_root}/memory.max")
    if used is None:                                         # cgroup v1
        used = _read_int(f"{cgroup_root}/memory/memory.usage_in_bytes")
        limit = _read_int(f"{cgroup_root}/memory/memory.limit_in_bytes")
    if limit is not None and limit > 1 << 50:                # "sem limite" no v1 é um número gigante
        limit = None
    if used is not None:                                     # como a Azure conta: sem o cache de arquivos ocioso
        for stat in (f"{cgroup_root}/memory.stat", f"{cgroup_root}/memory/memory.stat"):
            try:
                with open(stat) as fh:
                    vals = dict(line.split()[:2] for line in fh if len(line.split()) >= 2)
            except OSError:
                continue
            inactive = int(vals.get("inactive_file") or vals.get("total_inactive_file") or 0)
            used = max(0, used - inactive)
            break
    rss = None
    try:
        with open(proc_status) as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    rss = int(line.split()[1]) * 1024
                    break
    except (OSError, ValueError):
        pass
    mb = lambda v: None if v is None else round(v / 1048576)      # noqa: E731
    share = round(used / limit, 3) if used and limit else None
    return {"container_mb": mb(used), "limit_mb": mb(limit), "share": share, "process_mb": mb(rss)}


def backups_info() -> Optional[Dict[str, Any]]:
    """Cópias diárias dos dados (só na nuvem, onde o supervisor as guarda)."""
    import os
    from pathlib import Path
    base = os.environ.get("BYTE_DADOS_PERSISTENTES", "").strip()
    if not base:
        return None
    try:
        from .nuvem import list_backups
        items = list_backups(Path(base))
    except Exception:
        return None
    return {"count": len(items), "latest": items[0]["dia"] if items else "", "oldest": items[-1]["dia"] if items else ""}


def _recent_failures() -> List[Dict[str, Any]]:
    from .falhas import recent
    return recent()


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
        "cache": {"hits": SHARED.hits, "misses": SHARED.misses}, "ram": memory_usage(), "backups": backups_info(),
        "failures": _recent_failures(),
    }
    out["ok"] = out["llm"]["ready"] and not any(j["failures"] >= ALERT_AFTER for j in jobs) \
        and (out["ram"]["share"] or 0) < 0.9
    return out


def format_health(h: Dict[str, Any]) -> str:
    up = h["uptime_s"]
    lines = [f"{'🟢' if h['ok'] else '🟠'} {h['agent']} v{h['version']} · no ar há {up // 3600} h {up % 3600 // 60} min",
             f"🧠 Modelo: {h['llm']['model']} ({'pronto' if h['llm']['ready'] else 'FORA DO AR'})",
             f"🔎 Memória: {'por significado (Azure)' if 'azure' in h['memory_engine'] else 'por palavras' if h['memory_engine'] == 'hash-v1' else h['memory_engine']}"]
    ram = h.get("ram") or {}
    if ram.get("share") is not None:
        lines.append(f"{'⚠️' if ram['share'] >= MEMORY_WARN else '🧮'} Memória do servidor: {ram['container_mb']} de "
                     f"{ram['limit_mb']} MB ({round(ram['share'] * 100)}%)"
                     + (f" · este agente: {ram['process_mb']} MB" if ram.get("process_mb") else "")
                     + (" — perto do limite: suba o contêiner para 2 GiB ou desligue um agente" if ram["share"] >= MEMORY_WARN else ""))
    elif ram.get("process_mb"):
        lines.append(f"🧮 Memória deste agente: {ram['process_mb']} MB")
    bk = h.get("backups")
    if bk is not None:
        lines.append(f"💾 Cópias diárias dos dados: {bk['count']}" + (f" (de {bk['oldest']} a {bk['latest']})" if bk["count"] else
                                                                       " — a primeira sai no próximo salvamento"))
    if h["tokens_today"] is not None:
        lines.append(f"💸 Tokens hoje: {h['tokens_today']:,}".replace(",", ".")
                     + (f" de {h['token_budget']:,} da autonomia".replace(",", ".") if h.get("token_budget") else ""))
    if h["pending_approvals"]:
        lines.append(f"🔐 Aprovações esperando você: {h['pending_approvals']}")
    fails = h.get("failures") or []
    if fails:
        lines.append(f"🧯 Falhas contornadas nas últimas 24 h ({sum(f['vezes'] for f in fails)}):")
        for f in fails[:6]:
            lines.append(f"• {f['onde']}: {f['vezes']}x {_ago(int(time.time() - f['ultima']))} — {f['erro']}")
    if h["jobs"]:
        lines.append("⏱️ Tarefas de fundo:")
        for j in h["jobs"]:
            icon = "⚠️" if j["failures"] >= ALERT_AFTER else ("🟡" if j["failures"] else "✅")
            lines.append(f"{icon} {j['label']}: {_ago(j['last_run_s'])}"
                         + (f" — {j['failures']} falha(s): {j['last_error']}" if j["failures"] else ""))
    return "\n".join(lines)


# ---------------------------------------------------------------- a família inteira (v13.35, R1)
FAMILY_STALE_S = 900                     # v13.43: publica a cada 4 min (menos gravações no armazenamento)


def family_dir() -> Optional[Any]:
    """Pasta compartilhada pelos processos da nuvem (o armazenamento persistente). Fora da nuvem: nenhuma."""
    import os
    from pathlib import Path
    base = os.environ.get("BYTE_DADOS_PERSISTENTES", "").strip()
    return Path(base) / "saude" if base else None


def publish_health(service: Any) -> None:
    import json
    from . import engine
    folder = family_dir()
    if folder is None:
        return
    h = health(service)
    folder.mkdir(parents=True, exist_ok=True)
    agent_id = ((engine.ACTIVE_AGENT or {}).get("id") or "matrix").lower()
    data = {"agent": h["agent"], "version": h["version"], "ok": h["ok"], "at": time.time(), "llm_ready": h["llm"]["ready"],
            "failing": [j["label"] for j in h["jobs"] if j["failures"] >= ALERT_AFTER],
            "pending_approvals": h["pending_approvals"], "tokens_today": h["tokens_today"],
            "process_mb": h["ram"]["process_mb"], "container_share": h["ram"]["share"],
            "failures_24h": sum(f["vezes"] for f in h.get("failures") or []),
            "container_mb": h["ram"]["container_mb"], "limit_mb": h["ram"]["limit_mb"]}
    tmp = folder / f".{agent_id}.tmp"
    tmp.write_text(json.dumps(data, ensure_ascii=False))
    tmp.replace(folder / f"{agent_id}.json")


def family_health(now: Optional[float] = None) -> List[Dict[str, Any]]:
    import json
    folder = family_dir()
    if folder is None or not folder.is_dir():
        return []
    now = time.time() if now is None else now
    out = []
    for path in sorted(folder.glob("*.json")):
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        data["stale"] = now - float(data.get("at") or 0) > FAMILY_STALE_S
        out.append(data)
    return out


def format_family(items: List[Dict[str, Any]], now: Optional[float] = None) -> str:
    if not items:
        return "Ainda não há notícias dos outros agentes (só aparece na nuvem, depois do primeiro minuto)."
    now = time.time() if now is None else now
    lines = ["👨‍👩‍👧 Saúde dos agentes:"]
    for it in items:
        if it["stale"]:
            icon, state = "🔴", f"sem sinal há {int((now - float(it.get('at') or 0)) // 60)} min (parado ou reiniciando)"
        elif not it.get("llm_ready"):
            icon, state = "🟠", "no ar, mas o modelo está fora"
        elif it.get("failing"):
            icon, state = "🟠", "com falhas: " + ", ".join(it["failing"])
        else:
            icon, state = "🟢", "tudo certo"
        extra = f" · {it['pending_approvals']} aprovação(ões) esperando" if it.get("pending_approvals") else ""
        extra += f" · {it['process_mb']} MB" if it.get("process_mb") else ""
        extra += f" · {it['failures_24h']} falha(s) contornada(s) em 24 h" if it.get("failures_24h") else ""
        lines.append(f"{icon} {it['agent']} v{it.get('version', '?')}: {state}{extra}")
    fresh = [it for it in items if not it["stale"] and it.get("container_share") is not None]
    if fresh:
        top = max(fresh, key=lambda it: float(it.get("at") or 0))
        share = float(top["container_share"])
        lines.append(f"{'⚠️' if share >= MEMORY_WARN else '🧮'} Servidor: {top.get('container_mb')} de {top.get('limit_mb')} MB "
                     f"de memória ({round(share * 100)}%)" + (" — perto do limite" if share >= MEMORY_WARN else ""))
    return "\n".join(lines)


def memory_alert(service: Any) -> Optional[str]:
    """Na Matrix: avisa (no máximo a cada 12 h) quando o contêiner passa de 80% da memória."""
    ram = memory_usage()
    if ram["share"] is None or ram["share"] < MEMORY_WARN:
        return None
    last = float(service.db.get_setting("saude.alerta_memoria", 0) or 0)
    if time.time() - last < 12 * 3600:
        return None
    service.db.set_setting("saude.alerta_memoria", time.time())
    text = (f"⚠️ A memória do servidor está em {round(ram['share'] * 100)}% ({ram['container_mb']} de {ram['limit_mb']} MB). "
            "Perto de 100% o contêiner reinicia sozinho. Veja /saude todos; para folga, suba para 2 GiB "
            "(az containerapp update -g byte-rg -n byte --cpu 1 --memory 2Gi) ou desligue um agente.")
    service.db.enqueue_proactive_message("saude", text, brain_used="SAÚDE")
    return text


def install(service: Any, share: bool = True) -> Scheduler:
    """share=False no espaço de um convidado (v13.42): ele não publica a saúde no lugar do agente."""
    sched = Scheduler(lambda text: service.db.enqueue_proactive_message("saude", text, brain_used="SAÚDE"))
    service.scheduler = sched
    service.proactive_engine.background_jobs.append(sched.tick)
    if share and family_dir() is not None:                         # na nuvem: cada agente conta como está, a Matrix lê todos
        sched.add("saude_compartilhada", 240, lambda: publish_health(service), label="Saúde compartilhada")
        from . import engine as _engine
        if ((_engine.ACTIVE_AGENT or {}).get("id") or "matrix").lower() == "matrix":
            sched.add("memoria_servidor", 600, lambda: memory_alert(service), label="Memória do servidor")

    def act_health(payload: Dict[str, Any]) -> Dict[str, Any]:
        if payload.get("todos"):
            fam = family_health()
            return {"family": fam, "text": format_family(fam)}
        h = health(service)
        return dict(h, text=format_health(h))
    service.extra_actions["health"] = act_health
    service.status_providers["saude"] = lambda: {"ok": health(service)["ok"], "jobs": sched.status()}
    return sched

"""Gatilhos: a Byte age quando algo ACONTECE, não só num horário marcado — v13.14.

Vigias que rodam no relógio da autonomia (nenhum usa a LLM, então não gastam tokens):

  agenda            compromisso começando em breve (agenda do Google/Outlook pelo endereço secreto iCal, segredo
                    BYTE_AGENDA_ICS; aviso BYTE_GATILHO_AGENDA_MIN minutos antes, padrão 30)
  email             e-mail novo na caixa de entrada (se o e-mail estiver configurado). Só mostra remetente e
                    assunto; o conteúdo é de terceiros e nunca vira instrução. Filtro opcional BYTE_GATILHO_EMAIL_FILTRO
                    ("chefe@empresa.com,urgente": só avisa desses, os demais entram na contagem)
  tarefa_parada     tarefa pendente há BYTE_GATILHO_TAREFA_DIAS dias (padrão 5): sugere virar objetivo, reagendar ou concluir
  objetivo_travado  objetivo bloqueado há mais de um dia: lembra de destravar

Cada aviso tem uma chave: o mesmo evento não é avisado duas vezes (tarefas e objetivos voltam a lembrar depois de
alguns dias). Ligue/desligue com /gatilho <nome> on|off no Telegram; /gatilhos mostra o estado.
"""
from __future__ import annotations

import email
import imaplib
import os
import re
import ssl
import threading
import time
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

try:
    from zoneinfo import ZoneInfo
except ImportError:                                    # pragma: no cover
    ZoneInfo = None  # type: ignore

SCHEMA = """
CREATE TABLE IF NOT EXISTS trigger_log (
    key TEXT PRIMARY KEY,
    fired_at REAL NOT NULL
);
"""
MAX_ICS_BYTES = 5 * 1024 * 1024
DAY = 86400.0


@dataclass
class Event:
    key: str
    text: str
    repeat_s: float = 365 * DAY        # depois de quanto tempo o mesmo evento pode avisar de novo


def _env_float(name: str, default: float) -> float:
    try:
        return float(str(os.environ.get(name, "") or default).replace(",", "."))
    except ValueError:
        return default


def _tz() -> Any:
    name = os.environ.get("BYTE_FUSO", "America/Sao_Paulo")
    try:
        return ZoneInfo(name) if ZoneInfo else None
    except Exception:
        return None


def _local(epoch: float) -> datetime:
    tz = _tz()
    return datetime.fromtimestamp(epoch, tz) if tz else datetime.fromtimestamp(epoch)


def _clean(text: Any, limit: int) -> str:
    return " ".join(str(text or "").split())[:limit]


# ---------------------------------------------------------------- agenda (iCal)
_WEEKDAYS = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}


def _unfold(text: str) -> List[str]:
    lines: List[str] = []
    for raw in text.replace("\r\n", "\n").split("\n"):
        if raw.startswith((" ", "\t")) and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return lines


def _unescape(value: str) -> str:
    return value.replace("\\n", " ").replace("\\N", " ").replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\")


def _parse_dt(value: str, params: Dict[str, str]) -> Tuple[Optional[datetime], bool]:
    """-> (datetime com fuso, dia_inteiro). Aceita 20261120T150000Z, com TZID, ou só a data."""
    value = value.strip()
    try:
        if params.get("VALUE") == "DATE" or re.fullmatch(r"\d{8}", value):
            d = datetime.strptime(value[:8], "%Y%m%d")
            tz = _tz()
            return (d.replace(tzinfo=tz) if tz else d.astimezone()), True
        if value.endswith("Z"):
            return datetime.strptime(value[:15], "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc), False
        naive = datetime.strptime(value[:15], "%Y%m%dT%H%M%S")
        tzid = params.get("TZID")
        tz = None
        if tzid and ZoneInfo:
            try:
                tz = ZoneInfo(tzid)
            except Exception:
                tz = None
        tz = tz or _tz()
        return (naive.replace(tzinfo=tz) if tz else naive.astimezone()), False
    except ValueError:
        return None, False


def parse_ics(text: str) -> List[Dict[str, Any]]:
    """VEVENTs com início, título, local e recorrência (RRULE) — sem dependências externas."""
    events: List[Dict[str, Any]] = []
    cur: Optional[Dict[str, Any]] = None
    for line in _unfold(text):
        if line == "BEGIN:VEVENT":
            cur = {"exdates": set()}
            continue
        if line == "END:VEVENT":
            if cur and cur.get("start") and str(cur.get("status", "")).upper() != "CANCELLED":
                events.append(cur)
            cur = None
            continue
        if cur is None or ":" not in line:
            continue
        head, value = line.split(":", 1)
        name, *raw_params = head.split(";")
        params = dict(p.split("=", 1) for p in raw_params if "=" in p)
        name = name.upper()
        if name == "DTSTART":
            cur["start"], cur["all_day"] = _parse_dt(value, params)
        elif name == "SUMMARY":
            cur["title"] = _clean(_unescape(value), 160)
        elif name == "LOCATION":
            cur["location"] = _clean(_unescape(value), 120)
        elif name == "UID":
            cur["uid"] = _clean(value, 200)
        elif name == "STATUS":
            cur["status"] = value.strip()
        elif name == "RRULE":
            cur["rrule"] = dict(p.split("=", 1) for p in value.split(";") if "=" in p)
        elif name == "EXDATE":
            for v in value.split(","):
                dt, _ = _parse_dt(v, params)
                if dt:
                    cur["exdates"].add(dt.timestamp())
    return events


def occurrences(ev: Dict[str, Any], window_start: datetime, window_end: datetime, limit: int = 500) -> List[datetime]:
    """Inícios do evento dentro da janela. Recorrência: DAILY e WEEKLY (com BYDAY, INTERVAL, COUNT, UNTIL)."""
    start: datetime = ev["start"]
    rule = ev.get("rrule")
    if not rule:
        return [start] if window_start <= start < window_end else []
    freq = rule.get("FREQ", "").upper()
    if freq not in {"DAILY", "WEEKLY"}:
        return [start] if window_start <= start < window_end else []
    interval = max(1, int(rule.get("INTERVAL", "1") or 1))
    count = int(rule["COUNT"]) if rule.get("COUNT", "").isdigit() else None
    until = None
    if rule.get("UNTIL"):
        until, _ = _parse_dt(rule["UNTIL"], {})
    days = sorted({_WEEKDAYS[d[-2:]] for d in rule.get("BYDAY", "").split(",") if d[-2:] in _WEEKDAYS}) or [start.weekday()]
    out: List[datetime] = []
    n = 0
    day0 = start.date()
    for i in range(0, 3700):                            # até ~10 anos de dias
        d = day0 + timedelta(days=i)
        if freq == "DAILY":
            if i % interval:
                continue
        else:
            week = (d - (day0 - timedelta(days=day0.weekday()))).days // 7
            if week % interval or d.weekday() not in days:
                continue
        occ = start.replace(year=d.year, month=d.month, day=d.day)
        if occ < start:
            continue
        n += 1
        if (count is not None and n > count) or (until is not None and occ > until) or occ >= window_end:
            break
        if occ >= window_start and occ.timestamp() not in ev["exdates"]:
            out.append(occ)
            if len(out) >= limit:
                break
    return out


def fetch_ics(url: str, timeout: float = 20.0) -> str:
    if not re.match(r"^(https://|webcal://)", url.strip(), re.I):
        raise ValueError("BYTE_AGENDA_ICS precisa ser um endereço https:// (o endereço secreto iCal da agenda).")
    url = re.sub(r"^webcal://", "https://", url.strip(), flags=re.I)
    req = urllib.request.Request(url, headers={"User-Agent": "byte-agent-agenda/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read(MAX_ICS_BYTES + 1)
    if len(data) > MAX_ICS_BYTES:
        raise ValueError("agenda grande demais")
    return data.decode("utf-8", errors="replace")


# ---------------------------------------------------------------- motor
class TriggerEngine:
    LABELS = {"agenda": "compromisso chegando", "email": "e-mail novo", "tarefa_parada": "tarefa parada",
              "objetivo_travado": "objetivo travado"}
    INTERVALS = {"agenda": 600.0, "email": 900.0, "tarefa_parada": 6 * 3600.0, "objetivo_travado": 6 * 3600.0}

    def __init__(self, service: Any, notify: Callable[[str], None], read_secret: Optional[Callable[[Tuple[str, ...]], str]] = None,
                 clock: Callable[[], float] = time.time, ics_fetcher: Callable[[str], str] = fetch_ics,
                 mail_fetcher: Optional[Callable[[], List[Dict[str, str]]]] = None):
        self.service, self.db, self.notify, self.clock = service, service.db, notify, clock
        self.read_secret = read_secret or (lambda names: next((os.environ.get(n, "") for n in names if os.environ.get(n)), ""))
        self.ics_fetcher = ics_fetcher
        self.mail_fetcher = mail_fetcher or self._unseen_headers
        self.last_run: Dict[str, float] = {}
        self.last_error: Dict[str, Optional[str]] = {}
        self._busy = threading.Lock()
        with self.db.connection() as conn:
            conn.executescript(SCHEMA)

    # ---------------- estado
    def enabled(self) -> Dict[str, bool]:
        saved = self.db.get_setting("gatilhos", {}) or {}
        return {name: bool(saved.get(name, True)) for name in self.LABELS}

    def set_enabled(self, name: str, on: bool) -> Dict[str, bool]:
        if name not in self.LABELS:
            raise ValueError(f"Gatilho desconhecido. Use um de: {', '.join(self.LABELS)}.")
        saved = dict(self.db.get_setting("gatilhos", {}) or {})
        saved[name] = bool(on)
        self.db.set_setting("gatilhos", saved)
        return self.enabled()

    def configured(self) -> Dict[str, bool]:
        mail = getattr(self.service, "email_client", None)
        hub = getattr(self.service, "mail_hub", None)
        try:
            mail_ready = bool(hub and any(hub.ready(a) for a in hub.accounts())) or bool(mail and mail.status().get("credentials_ready"))
        except Exception:
            mail_ready = False
        return {"agenda": bool(self.read_secret(("BYTE_AGENDA_ICS",))), "email": mail_ready,
                "tarefa_parada": True, "objetivo_travado": getattr(self.service, "goal_runner", None) is not None}

    def status(self) -> Dict[str, Any]:
        on, ready = self.enabled(), self.configured()
        return {"triggers": [{"name": n, "label": lbl, "enabled": on[n], "configured": ready[n],
                              "last_error": self.last_error.get(n)} for n, lbl in self.LABELS.items()]}

    def _fired_recently(self, key: str, repeat_s: float, now: float) -> bool:
        with self.db.connection() as conn:
            row = conn.execute("SELECT fired_at FROM trigger_log WHERE key = ?", (key,)).fetchone()
        return bool(row) and now - float(row["fired_at"]) < repeat_s

    def _mark(self, keys: List[str], now: float) -> None:
        with self.db.connection() as conn:
            conn.executemany("INSERT INTO trigger_log (key, fired_at) VALUES (?, ?) "
                             "ON CONFLICT(key) DO UPDATE SET fired_at = excluded.fired_at", [(k, now) for k in keys])
            conn.execute("DELETE FROM trigger_log WHERE fired_at < ?", (now - 400 * DAY,))

    # ---------------- relógio
    def tick(self, force: bool = False) -> List[str]:
        """Roda os vigias que estão na hora. Devolve os textos avisados."""
        if not self._busy.acquire(blocking=False):
            return []
        sent: List[str] = []
        try:
            now = self.clock()
            on, ready = self.enabled(), self.configured()
            for name in self.LABELS:
                if not (on[name] and ready[name]):
                    continue
                if not force and now - self.last_run.get(name, 0.0) < self.INTERVALS[name]:
                    continue
                self.last_run[name] = now
                try:
                    events = getattr(self, f"_watch_{name}")(now)
                    self.last_error[name] = None
                except Exception as err:                 # um vigia com problema não derruba os outros
                    self.last_error[name] = f"{type(err).__name__}: {str(err)[:160]}"
                    continue
                fresh = [e for e in events if not self._fired_recently(e.key, e.repeat_s, now)]
                if not fresh:
                    continue
                text = fresh[0].text if len(fresh) == 1 else self._group(name, fresh)
                self.notify(text)
                self._mark([e.key for e in fresh], now)
                sent.append(text)
        finally:
            self._busy.release()
        return sent

    @staticmethod
    def _group(name: str, events: List[Event]) -> str:
        head = {"agenda": "📅 Compromissos chegando:", "email": f"📬 {len(events)} e-mails novos:",
                "tarefa_parada": "📌 Tarefas paradas há dias:", "objetivo_travado": "🧩 Objetivos travados:"}[name]
        lines = [e.text.split("\n", 1)[0].lstrip("📅📬📌🧩 ") for e in events[:8]]
        more = f"\n…e mais {len(events) - 8}." if len(events) > 8 else ""
        tail = {"tarefa_parada": "\nQuer que eu transforme alguma em objetivo (/objetivo), reagende ou dê como concluída?",
                "objetivo_travado": "\nMe diga como seguir e use /retomar <id>."}.get(name, "")
        return head + "\n" + "\n".join(f"• {ln}" for ln in lines) + more + tail

    # ---------------- vigias
    def _watch_agenda(self, now: float) -> List[Event]:
        lead = max(1.0, _env_float("BYTE_GATILHO_AGENDA_MIN", 30.0)) * 60.0
        text = self.ics_fetcher(self.read_secret(("BYTE_AGENDA_ICS",)))
        start = datetime.fromtimestamp(now, timezone.utc)
        end = datetime.fromtimestamp(now + lead, timezone.utc)
        out: List[Event] = []
        for ev in parse_ics(text):
            if ev.get("all_day"):
                continue                                  # evento de dia inteiro não tem "começa em 30 min"
            for occ in occurrences(ev, start, end):
                mins = max(0, int(round((occ.timestamp() - now) / 60)))
                where = f" — {ev['location']}" if ev.get("location") else ""
                out.append(Event(f"agenda|{ev.get('uid') or ev.get('title')}|{int(occ.timestamp())}",
                                 f"📅 Em {mins} min ({_local(occ.timestamp()):%H:%M}): {ev.get('title') or 'compromisso'}{where}"))
        return out

    def _unseen_headers(self) -> List[Dict[str, str]]:
        hub = getattr(self.service, "mail_hub", None)
        if hub is not None and hub.accounts():                  # v13.27: todas as caixas, cada uma com a sua chave
            return [{"uid": h["ref"], "account": h["account"], "from": h["from"], "subject": h["subject"]}
                    for h in hub.headers(None, unread_only=True, limit=30)]
        mail = self.service.email_client
        password, _ = mail._resolve_password()
        out: List[Dict[str, str]] = []
        with imaplib.IMAP4_SSL(mail.imap_host, mail.imap_port, ssl_context=ssl.create_default_context(), timeout=12) as conn:
            mail._imap_login(conn, password)
            conn.select("INBOX", readonly=True)
            _, data = conn.uid("SEARCH", None, "UNSEEN")
            uids = (data[0].split() if data and data[0] else [])[-30:]
            for uid in uids:
                _, msg = conn.uid("FETCH", uid, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT)])")
                raw = next((p[1] for p in (msg or []) if isinstance(p, tuple) and len(p) > 1), b"")
                hdr = email.message_from_bytes(raw)
                out.append({"uid": uid.decode(), "from": mail._decode_mime_header(hdr.get("From", "")),
                            "subject": mail._decode_mime_header(hdr.get("Subject", ""))})
        return out

    def _watch_email(self, now: float) -> List[Event]:
        headers = self.mail_fetcher()
        # 1ª vez de cada caixa: o que já estava não lido não vira aviso (senão chegariam dezenas de mensagens de uma vez)
        hub = getattr(self.service, "mail_hub", None)
        ids = {a.id for a in hub.accounts()} if hub is not None and hub.accounts() else {""}
        if hub is not None:                                       # caixa que não foi lida agora (sem login) não "começa"
            ids -= {str(e).split(":", 1)[0] for e in getattr(hub, "last_errors", [])}
        ids |= {str(h.get("account") or "") for h in headers}
        started = set(self.db.get_setting("gatilho_email_contas", []) or [])
        if self.db.get_setting("gatilho_email_inicio"):
            started.add("")                                       # a conta única de antes (v13.14) já tinha começado
        fresh = ids - started
        if fresh:
            self._mark([f"email|{h['uid']}" for h in headers if str(h.get("account") or "") in fresh], now)
            self.db.set_setting("gatilho_email_contas", sorted(started | fresh))
            if "" in fresh:
                self.db.set_setting("gatilho_email_inicio", int(now))
            headers = [h for h in headers if str(h.get("account") or "") not in fresh]
        words = [w.strip().lower() for w in os.environ.get("BYTE_GATILHO_EMAIL_FILTRO", "").split(",") if w.strip()]
        out: List[Event] = []
        for h in headers:
            who, subject = _clean(h.get("from"), 80), _clean(h.get("subject"), 120) or "(sem assunto)"
            hay = f"{who} {subject}".lower()
            if words and not any(w in hay for w in words):
                self._mark([f"email|{h['uid']}"], now)           # fora do filtro: só registra
                continue
            where = f" ({h['account']})" if h.get("account") else ""
            out.append(Event(f"email|{h['uid']}", f"📬 E-mail novo{where} de {who}: {subject}"))
        return out

    def _watch_tarefa_parada(self, now: float) -> List[Event]:
        days = max(1.0, _env_float("BYTE_GATILHO_TAREFA_DIAS", 5.0))
        out: List[Event] = []
        for t in self.db.list_tasks(status="pending"):
            try:
                created = datetime.fromisoformat(str(t["created_at"]).replace("Z", "+00:00"))
            except ValueError:
                continue
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            age = (now - created.timestamp()) / DAY
            if age >= days:
                out.append(Event(f"tarefa|{t['id']}", f"📌 \"{_clean(t['title'], 120)}\" está pendente há {int(age)} dias.\n"
                                 "Quer que eu transforme em objetivo (/objetivo), reagende ou dê como concluída?",
                                 repeat_s=7 * DAY))
        return out

    def _watch_objetivo_travado(self, now: float) -> List[Event]:
        runner = getattr(self.service, "goal_runner", None)
        out: List[Event] = []
        for g in runner.store.list() if runner else []:
            if g["status"] != "blocked":
                continue
            try:
                updated = datetime.fromisoformat(str(g["updated_at"]))
            except ValueError:
                continue
            if now - updated.timestamp() >= DAY:
                out.append(Event(f"objetivo|{g['id']}", f"🧩 O objetivo #{g['id']} \"{g['title']}\" continua travado.\n"
                                 f"Me diga como seguir e use /retomar {g['id']} (ou /cancelar_objetivo {g['id']}).",
                                 repeat_s=2 * DAY))
        return out


def format_status(st: Dict[str, Any]) -> str:
    lines = ["Gatilhos (a Byte avisa quando algo acontece):"]
    for t in st["triggers"]:
        state = "ligado" if t["enabled"] else "desligado"
        if t["enabled"] and not t["configured"]:
            state += " (falta configurar)"
        err = f" ⚠️ {t['last_error']}" if t.get("last_error") else ""
        lines.append(f"• {t['name']} — {t['label']}: {state}{err}")
    lines.append("Ligar/desligar: /gatilho <nome> on|off")
    return "\n".join(lines)


def install(service: Any) -> TriggerEngine:
    from . import engine as _engine

    def notify(text: str) -> None:
        service.db.enqueue_proactive_message("trigger", text, brain_used="GATILHO")

    trig = TriggerEngine(service, notify, read_secret=getattr(_engine, "_read_secret", None))
    service.trigger_engine = trig
    state = {"thread": None}

    def tick_in_background() -> None:
        th = state["thread"]
        if th is not None and th.is_alive():
            return
        th = state["thread"] = threading.Thread(target=trig.tick, daemon=True, name="ByteGatilhos")
        th.start()

    service.proactive_engine.background_jobs.append(tick_in_background)
    service.extra_actions.update({
        "trigger_list": lambda payload: dict(trig.status(), text=format_status(trig.status())),
        "trigger_toggle": lambda payload: {"enabled": trig.set_enabled(str(payload.get("name") or ""), bool(payload.get("on")))},
        "trigger_run": lambda payload: {"sent": trig.tick(force=True)},
    })
    service.status_providers["gatilhos"] = trig.status
    return trig

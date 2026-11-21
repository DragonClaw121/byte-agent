"""Rotina: o agente aprende seus horários e lugares e sugere no momento certo — Fase 3 (v13.29).

O que ele aprende (sem a LLM, sem gastar tokens):
  atividade   em que horas de cada dia da semana você fala com ele (começo e fim do seu dia)
  lugares     por quais áreas de ~1 km você passa e em que horas (casa = onde você está de madrugada; trabalho = onde
              você fica nos dias úteis em horário comercial). Só CONTAGENS por área e hora da semana: o trajeto não é
              guardado. Vem da localização do app do Kronus (opção "Compartilhar minha localização").
  saídas      a hora em que você costuma sair de casa em cada dia da semana
  repetições  lembretes que você pede toda semana no mesmo dia e horário
As contagens "esquecem" devagar (perdem metade do peso a cada ~3 semanas): mudar de rotina muda as sugestões.

O que ele sugere:
  ☔ chuva perto da hora em que você costuma sair (45 min antes)
  🚗 hora de sair para um compromisso da agenda, pela distância de onde você está (estimativa de trânsito urbano)
  🔁 "toda segunda você pede X: quer um lembrete fixo?" (/rotina aceitar <n>)
  ⏰ resumo da manhã no horário em que você realmente começa o dia
  💡 no resumo da manhã: horário livre na agenda para a tarefa mais importante

Limites: no máximo 4 sugestões por dia e nenhuma fora do seu horário ativo (chuva na saída e "hora de sair" valem sempre).
/rotina mostra o que foi aprendido · /rotina off|on · /rotina esquecer apaga tudo.
"""
from __future__ import annotations

import math
import re
import statistics
import threading
import time
import unicodedata
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import gatilhos

SCHEMA = """
CREATE TABLE IF NOT EXISTS rotina_sinais (
    kind TEXT NOT NULL,
    hour_of_week INTEGER NOT NULL,
    weight REAL NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (kind, hour_of_week)
);
"""
HALF_LIFE_S = 21 * 86400.0
DAY = 86400.0
DIAS = ["segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo"]
DIAS_PLURAL = ["segundas", "terças", "quartas", "quintas", "sextas", "sábados", "domingos"]
MAX_PER_DAY = 4
STOPWORDS = {"de", "da", "do", "das", "dos", "a", "o", "as", "os", "e", "me", "lembrar", "lembre", "lembra", "lembrete",
             "para", "pra", "que", "eu", "um", "uma", "no", "na", "hoje", "amanha", "agora", "ao"}


def cell_of(lat: float, lon: float) -> str:
    return f"{round(lat, 2):.2f},{round(lon, 2):.2f}"          # ~1 km


def _fold(text: str) -> str:
    t = unicodedata.normalize("NFKD", str(text or "").lower())
    return "".join(c for c in t if not unicodedata.combining(c))


def text_key(text: str) -> str:
    words = [w for w in re.findall(r"[a-z]+", _fold(text)) if w not in STOPWORDS and len(w) > 2]
    return " ".join(sorted(set(words))[:6])


def km(a: Tuple[float, float], b: Tuple[float, float]) -> float:
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 6371.0 * 2 * math.asin(min(1.0, math.sqrt(h)))


def fmt_hour(h: float) -> str:
    hh, mm = int(h), int(round((h - int(h)) * 60))
    if mm == 60:
        hh, mm = hh + 1, 0
    return f"{hh}h" + (f"{mm:02d}" if mm else "")


class Routine:
    def __init__(self, service: Any, clock: Callable[[], float] = time.time, getter: Optional[Callable[[str], Any]] = None,
                 ics_fetcher: Callable[[str], str] = gatilhos.fetch_ics):
        self.service, self.db, self.clock, self.ics_fetcher = service, service.db, clock, ics_fetcher
        self.getter = getter
        self.notify: Callable[[str], None] = lambda text: self.db.enqueue_proactive_message("rotina", text, brain_used="ROTINA")
        self.last_error: Optional[str] = None
        self._busy = threading.Lock()
        with self.db.connection() as conn:
            conn.executescript(SCHEMA)
            conn.executescript(gatilhos.SCHEMA)

    # ---------------- liga/desliga
    def enabled(self) -> bool:
        return bool(self.db.get_setting("rotina_ativa", True))

    def set_enabled(self, on: bool) -> None:
        self.db.set_setting("rotina_ativa", bool(on))

    def forget(self) -> None:
        with self.db.connection() as conn:
            conn.execute("DELETE FROM rotina_sinais")
            conn.execute("DELETE FROM trigger_log WHERE key LIKE 'rotina|%'")
        for key in ("rotina_sugestoes", "rotina_ultima_area", "rotina_dia"):
            self.db.set_setting(key, None)

    # ---------------- sinais
    def _local(self, epoch: Optional[float] = None) -> datetime:
        return gatilhos._local(self.clock() if epoch is None else epoch)

    def record(self, kind: str, epoch: Optional[float] = None, amount: float = 1.0) -> None:
        if not self.enabled():
            return
        now = self.clock() if epoch is None else epoch
        d = self._local(now)
        how = d.weekday() * 24 + d.hour
        with self.db.connection() as conn:
            row = conn.execute("SELECT weight, updated_at FROM rotina_sinais WHERE kind = ? AND hour_of_week = ?", (kind, how)).fetchone()
            weight = amount
            if row:
                weight += float(row["weight"]) * 0.5 ** (max(0.0, now - float(row["updated_at"])) / HALF_LIFE_S)
            conn.execute("INSERT INTO rotina_sinais (kind, hour_of_week, weight, updated_at) VALUES (?, ?, ?, ?) "
                         "ON CONFLICT(kind, hour_of_week) DO UPDATE SET weight = excluded.weight, updated_at = excluded.updated_at",
                         (kind, how, weight, now))

    def record_location(self, lat: float, lon: float, epoch: Optional[float] = None) -> None:
        """Conta a área (~1 km) por hora da semana e, se mudou de área, a SAÍDA da anterior. Não guarda trajeto."""
        if not self.enabled():
            return
        now = self.clock() if epoch is None else epoch
        here = cell_of(lat, lon)
        last = self.db.get_setting("rotina_ultima_area") or {}
        same_hour = last.get("cell") == here and now - float(last.get("at") or 0) < 3000
        if not same_hour:                                   # uma contagem por hora no mesmo lugar (o app manda a cada 15 min)
            self.record(f"lugar:{here}", now)
        if last.get("cell") and last["cell"] != here and now - float(last.get("at") or 0) < 3 * 3600:
            self.record(f"saida:{last['cell']}", now)
        if not same_hour or last.get("cell") != here:
            self.db.set_setting("rotina_ultima_area", {"cell": here, "at": now})

    def weights(self, prefix: str) -> Dict[str, Dict[int, float]]:
        now = self.clock()
        out: Dict[str, Dict[int, float]] = {}
        with self.db.connection() as conn:
            rows = conn.execute("SELECT kind, hour_of_week, weight, updated_at FROM rotina_sinais WHERE kind LIKE ?",
                                (prefix + "%",)).fetchall()
        for r in rows:
            w = float(r["weight"]) * 0.5 ** (max(0.0, now - float(r["updated_at"])) / HALF_LIFE_S)
            if w >= 0.05:
                out.setdefault(r["kind"], {})[int(r["hour_of_week"])] = w
        return out

    # ---------------- o que foi aprendido
    def home(self) -> Optional[str]:
        best, score = None, 0.0
        for kind, hours in self.weights("lugar:").items():
            s = sum(w for how, w in hours.items() if how % 24 in (0, 1, 2, 3, 4, 5, 22, 23))
            if s > score:
                best, score = kind.split(":", 1)[1], s
        return best if score >= 3 else None

    def work(self) -> Optional[str]:
        home = self.home()
        best, score = None, 0.0
        for kind, hours in self.weights("lugar:").items():
            cell = kind.split(":", 1)[1]
            if cell == home:
                continue
            s = sum(w for how, w in hours.items() if how // 24 < 5 and 9 <= how % 24 <= 17)
            if s > score:
                best, score = cell, s
        return best if score >= 4 else None

    def leaving_hour(self, weekday: int) -> Optional[float]:
        """Hora típica (ex.: 7.5) em que você sai de casa nesse dia da semana, ou None se ainda não há padrão."""
        home = self.home()
        if not home:
            return None
        hours = self.weights(f"saida:{home}").get(f"saida:{home}", {})
        day = {how % 24: w for how, w in hours.items() if how // 24 == weekday and 5 <= how % 24 <= 13}
        if not day or max(day.values()) < 1.5:                    # ~3 semanas seguidas (as contagens esquecem devagar)
            return None
        h = max(day, key=day.get)
        return h + 0.5                                           # meio da hora em que mais saídas aconteceram

    def active_window(self, weekday: int) -> Tuple[int, int]:
        """(primeira, última) hora em que você costuma falar com o agente nesse dia. Padrão 7h–22h."""
        hours = self.weights("atividade").get("atividade", {})
        day = sorted(how % 24 for how, w in hours.items() if how // 24 == weekday and w >= 1.5 and how % 24 >= 5)
        if len(day) < 3:
            return 7, 22
        return day[0], max(day[-1], day[0] + 6)

    # ---------------- regras
    def _quiet(self, now: datetime) -> bool:
        first, last = self.active_window(now.weekday())
        return not (first <= now.hour <= min(23, last + 1))

    def _rain_soon(self, start_h: float, hours: int = 3) -> Optional[Tuple[int, str]]:
        local = getattr(self.service, "local", None)
        if local is None:
            return None
        place = local.where("")
        getter = self.getter or local.getter
        res = getter("https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode({
            "latitude": place["lat"], "longitude": place["lon"], "timezone": "auto",
            "hourly": "precipitation_probability", "forecast_days": 1}))
        hourly = res.get("hourly") or {}
        best = None
        for t, p in zip(hourly.get("time") or [], hourly.get("precipitation_probability") or []):
            h = int(str(t)[11:13] or 0)
            if p is not None and start_h - 0.5 <= h <= start_h + hours and (best is None or p > best[0]):
                best = (int(p), str(t)[11:16])
        return best

    def rule_rain_before_leaving(self, now: datetime) -> List[gatilhos.Event]:
        h = self.leaving_hour(now.weekday())
        if h is None:
            return []
        leave = now.replace(hour=int(h), minute=int((h % 1) * 60), second=0, microsecond=0)
        if not (leave - timedelta(minutes=50) <= now <= leave - timedelta(minutes=10)):
            return []
        loc = self.db.get_setting("rotina_ultima_area") or {}
        if loc.get("cell") and loc["cell"] != self.home():      # já saiu de casa
            return []
        rain = self._rain_soon(h)
        if not rain or rain[0] < 50:
            return []
        return [gatilhos.Event(f"rotina|chuva|{now:%Y-%m-%d}",
                               f"☔ Você costuma sair por volta das {fmt_hour(h)} e há {rain[0]}% de chance de chuva "
                               f"(por volta das {rain[1]}). Leve o guarda-chuva!", repeat_s=DAY)]

    def _geocode_place(self, text: str) -> Optional[Tuple[float, float]]:
        cache = dict(self.db.get_setting("rotina_lugares_agenda", {}) or {})
        key = _fold(text)[:120]
        if key in cache:
            return tuple(cache[key]) if cache[key] else None
        local = getattr(self.service, "local", None)
        getter = self.getter or (local.getter if local else None)
        if getter is None:
            return None
        try:
            res = getter("https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(
                {"q": text, "format": "jsonv2", "limit": 1, "countrycodes": "br", "accept-language": "pt-BR"}))
            hit = (res or [None])[0]
            point = (float(hit["lat"]), float(hit["lon"])) if hit else None
        except Exception:
            return None
        cache[key] = list(point) if point else None
        if len(cache) > 200:
            cache = dict(list(cache.items())[-200:])
        self.db.set_setting("rotina_lugares_agenda", cache)
        return point

    def rule_time_to_leave(self, now_epoch: float) -> List[gatilhos.Event]:
        local = getattr(self.service, "local", None)
        here = local.current() if local else None
        ics = self._ics()
        if not (here and ics):
            return []
        start = datetime.fromtimestamp(now_epoch, timezone.utc)
        out: List[gatilhos.Event] = []
        for ev in gatilhos.parse_ics(self.ics_fetcher(ics)):
            if ev.get("all_day") or not ev.get("location"):
                continue
            for occ in gatilhos.occurrences(ev, start, start + timedelta(hours=3)):
                dest = self._geocode_place(ev["location"])
                if not dest:
                    continue
                dist = km((here["lat"], here["lon"]), dest)
                if dist < 0.8:
                    continue                                      # você já está lá (ou ao lado)
                # estimativa: 10 min para sair + cidade (~28 km/h) nos primeiros 10 km + estrada (~70 km/h) no resto
                travel = int(10 + min(dist, 10) * 60 / 28 + max(0.0, dist - 10) * 60 / 70)
                leave = occ.timestamp() - (travel + 10) * 60
                if now_epoch >= leave and now_epoch < occ.timestamp():
                    late = now_epoch > occ.timestamp() - travel * 60
                    t = gatilhos._local(occ.timestamp())
                    title = ev.get("title") or "compromisso"
                    if late:
                        text = (f"🚗 Já devia ter saído: \"{title}\" às {t:%H:%M} fica a ~{dist:.0f} km daqui "
                                f"(≈{travel} min). Vai se atrasar uns {int((now_epoch + travel * 60 - occ.timestamp()) // 60) + 1} min.")
                    else:
                        text = (f"🚗 Hora de sair: \"{title}\" às {t:%H:%M} em {ev['location'][:80]} fica a ~{dist:.0f} km "
                                f"daqui (≈{travel} min). Saia até {gatilhos._local(leave + 10 * 60):%H:%M}.")
                    out.append(gatilhos.Event(f"rotina|sair|{ev.get('uid') or title}|{int(occ.timestamp())}", text))
        return out

    def repeated_reminders(self) -> List[Dict[str, Any]]:
        """Lembretes avulsos pedidos em 3+ semanas diferentes, no mesmo dia da semana e por volta da mesma hora."""
        now = self.clock()
        groups: Dict[Tuple[str, int], List[Dict[str, Any]]] = {}
        for r in self.db.list_reminders(include_done=True):
            if int(r.get("repeat_s") or 0) or now - float(r["due_epoch"]) > 8 * 7 * DAY:
                continue
            key = text_key(r["text"])
            if not key:
                continue
            d = self._local(float(r["due_epoch"]))
            groups.setdefault((key, d.weekday()), []).append({"text": r["text"], "dt": d})
        active = {text_key(r["text"]) for r in self.db.list_reminders() if int(r.get("repeat_s") or 0)}
        out = []
        for (key, wd), items in groups.items():
            weeks = {i["dt"].isocalendar()[:2] for i in items}
            hours = [i["dt"].hour + i["dt"].minute / 60 for i in items]
            if len(weeks) < 3 or key in active or max(hours) - min(hours) > 1.5:
                continue
            med = statistics.median(hours)
            out.append({"key": key, "weekday": wd, "hour": round(med * 4) / 4, "text": items[-1]["text"], "count": len(weeks)})
        return out

    def rule_repeated(self, now: datetime) -> List[gatilhos.Event]:
        found = self.repeated_reminders()
        if not found:
            return []
        saved = dict(self.db.get_setting("rotina_sugestoes", {}) or {})
        events = []
        for item in found:
            sid = next((k for k, v in saved.items() if v.get("key") == item["key"] and v.get("weekday") == item["weekday"]), None)
            if sid is None:
                sid = str(max([int(k) for k in saved] or [0]) + 1)
                saved[sid] = item
            events.append(gatilhos.Event(
                f"rotina|repete|{item['key']}|{item['weekday']}",
                f"🔁 Nas últimas {item['count']} semanas, às {DIAS_PLURAL[item['weekday']]} por volta das {fmt_hour(item['hour'])}, "
                f"você me pediu: \"{item['text'][:80]}\". Quer um lembrete fixo toda semana? Responda /rotina aceitar {sid}",
                repeat_s=30 * DAY))
        self.db.set_setting("rotina_sugestoes", saved)
        return events

    def rule_briefing_time(self, now: datetime) -> List[gatilhos.Event]:
        brief = getattr(self.service, "briefings", None)
        if brief is None:
            return []
        cfg = brief.config()["manha"]
        if not cfg["ativo"]:
            return []
        firsts = []
        for wd in range(5):
            hours = self.weights("atividade").get("atividade", {})
            day = sorted(how % 24 for how, w in hours.items() if how // 24 == wd and w >= 1.5 and how % 24 >= 5)
            if day:
                firsts.append(day[0])
        if len(firsts) < 3:
            return []
        typical = statistics.median(firsts)
        h, m = (int(x) for x in cfg["hora"].split(":"))
        if abs(typical - (h + m / 60)) < 1.25:
            return []
        suggest = f"{int(typical):02d}:00" if typical > h else f"{max(5, int(typical) - 1):02d}:45"
        return [gatilhos.Event("rotina|resumo-hora", f"⏰ Nos dias úteis você costuma começar a falar comigo por volta das "
                               f"{fmt_hour(typical)}, mas o resumo da manhã sai às {cfg['hora']}. Quer mudar? "
                               f"/resumo hora {suggest}", repeat_s=30 * DAY)]

    # ---------------- resumo da manhã: horário livre
    def _ics(self) -> str:
        read = getattr(getattr(self.service, "trigger_engine", None), "read_secret", None)
        if read is None:
            from . import engine
            read = engine._read_secret
        try:
            return read(("BYTE_AGENDA_ICS",)) or ""
        except Exception:
            return ""

    def _fired(self, key: str, repeat_s: float, now: float) -> bool:
        with self.db.connection() as conn:
            row = conn.execute("SELECT fired_at FROM trigger_log WHERE key = ?", (key,)).fetchone()
        return bool(row) and now - float(row["fired_at"]) < repeat_s

    def _mark(self, key: str, now: float) -> None:
        with self.db.connection() as conn:
            conn.execute("INSERT INTO trigger_log (key, fired_at) VALUES (?, ?) "
                         "ON CONFLICT(key) DO UPDATE SET fired_at = excluded.fired_at", (key, now))

    def free_slots(self, now: datetime) -> List[Tuple[datetime, datetime]]:
        ics = self._ics()
        if not ics:
            return []
        first, last = self.active_window(now.weekday())
        day0 = now.replace(hour=max(8, first), minute=0, second=0, microsecond=0)
        day1 = now.replace(hour=min(19, max(last, 18)), minute=0, second=0, microsecond=0)
        busy = []
        for ev in gatilhos.parse_ics(self.ics_fetcher(ics)):
            if ev.get("all_day"):
                continue
            for occ in gatilhos.occurrences(ev, day0, day1):
                busy.append((occ, occ + timedelta(hours=1)))
        busy.sort()
        free, cursor = [], max(day0, now)
        for b0, b1 in busy:
            if b0 - cursor >= timedelta(minutes=90):
                free.append((cursor, b0))
            cursor = max(cursor, b1)
        if day1 - cursor >= timedelta(minutes=90):
            free.append((cursor, day1))
        return free

    def briefing_lines(self, now: datetime) -> List[str]:
        if not self.enabled():
            return []
        lines = []
        try:
            h = self.leaving_hour(now.weekday())
            if h is not None:
                rain = self._rain_soon(h)
                if rain and rain[0] >= 50:
                    lines.append(f"☔ Chuva ({rain[0]}%) perto da hora em que você costuma sair ({fmt_hour(h)}): leve guarda-chuva.")
        except Exception:
            pass
        try:
            tasks = self.db.list_tasks(status="pending")
            slots = self.free_slots(now)
            if tasks and slots:
                s0, s1 = max(slots, key=lambda s: s[1] - s[0])
                lines.append(f"💡 Você tem um horário livre das {s0:%H:%M} às {s1:%H:%M}: bom momento para \"{tasks[0]['title'][:80]}\".")
        except Exception:
            pass
        return lines

    # ---------------- relógio
    def tick(self) -> List[str]:
        if not self.enabled() or not self._busy.acquire(blocking=False):
            return []
        sent: List[str] = []
        try:
            now_epoch = self.clock()
            now = self._local(now_epoch)
            day = dict(self.db.get_setting("rotina_dia", {}) or {})
            if day.get("dia") != now.strftime("%Y-%m-%d"):
                day = {"dia": now.strftime("%Y-%m-%d"), "n": 0}
            rules: List[Tuple[str, Callable[[], List[gatilhos.Event]]]] = [
                ("sair", lambda: self.rule_time_to_leave(now_epoch)),
                ("chuva", lambda: self.rule_rain_before_leaving(now)),
                ("repete", lambda: self.rule_repeated(now)),
                ("resumo", lambda: self.rule_briefing_time(now)),
            ]
            for name, rule in rules:
                timed = name in {"sair", "chuva"}                 # presos a um horário seu: valem fora do horário ativo
                if not timed and (self._quiet(now) or day["n"] >= MAX_PER_DAY):
                    continue
                try:
                    events = rule()
                except Exception as err:
                    self.last_error = f"{name}: {type(err).__name__}: {str(err)[:140]}"
                    continue
                for ev in events:
                    if self._fired(ev.key, ev.repeat_s, now_epoch):
                        continue
                    if not timed and day["n"] >= MAX_PER_DAY:
                        break
                    self.notify(ev.text)
                    self._mark(ev.key, now_epoch)
                    sent.append(ev.text)
                    day["n"] += 1
            self.db.set_setting("rotina_dia", day)
        finally:
            self._busy.release()
        return sent

    # ---------------- aceitar sugestão
    def accept(self, sid: str) -> str:
        saved = dict(self.db.get_setting("rotina_sugestoes", {}) or {})
        item = saved.get(str(sid).strip().lstrip("#"))
        if not item:
            raise ValueError("Sugestão não encontrada. Veja as sugestões em /rotina.")
        now = self._local()
        target = now.replace(hour=int(item["hour"]), minute=int((item["hour"] % 1) * 60), second=0, microsecond=0)
        days = (item["weekday"] - now.weekday()) % 7
        target += timedelta(days=days)
        if target <= now:
            target += timedelta(days=7)
        rem = self.db.add_reminder(item["text"], target.timestamp(), kind="note", repeat="weekly")
        saved.pop(str(sid).strip().lstrip("#"), None)
        self.db.set_setting("rotina_sugestoes", saved)
        self.db.record_audit("acao_automatica", "rotina", "success", {"resumo": f"Lembrete semanal: {item['text'][:80]}"})
        return (f"✅ Lembrete fixo criado: \"{item['text'][:80]}\" toda {DIAS[item['weekday']]} às {target:%H:%M} "
                f"(#{rem['id']}; cancele com /cancelar {rem['id']}).")

    # ---------------- resumo do que foi aprendido
    def describe(self) -> str:
        if not self.enabled():
            return "Rotina desligada. Ligue com /rotina on."
        lines = ["🧭 O que aprendi da sua rotina:"]
        local = getattr(self.service, "local", None)
        home, work = self.home(), self.work()
        place = ""
        if home and local is not None:
            loc = local.db.get_setting("localizacao_atual") or {}
            if loc and cell_of(loc["lat"], loc["lon"]) == home:
                place = f" ({loc.get('bairro') or loc.get('cidade') or 'aqui'})"
        lines.append(f"🏠 Casa: {'identificada' + place if home else 'ainda não (preciso da localização do app por algumas noites)'}")
        lines.append(f"🏢 Trabalho/estudo: {'identificado' if work else 'ainda não'}")
        leaves = [f"{DIAS[wd][:3]} {fmt_hour(h)}" for wd in range(7) if (h := self.leaving_hour(wd)) is not None]
        lines.append("🚪 Costuma sair de casa: " + (", ".join(leaves) if leaves else "ainda sem padrão"))
        win = [f"{DIAS[wd][:3]} {a}h–{b}h" for wd in range(7) for a, b in [self.active_window(wd)] if (a, b) != (7, 22)]
        lines.append("🕒 Horário ativo: " + (", ".join(win) if win else "ainda aprendendo (padrão 7h–22h)"))
        sugg = self.db.get_setting("rotina_sugestoes", {}) or {}
        if sugg:
            lines.append("🔁 Sugestões abertas: " + "; ".join(
                f"#{k} \"{v['text'][:40]}\" ({DIAS_PLURAL[v['weekday']]} {fmt_hour(v['hour'])})" for k, v in sugg.items()))
        if self.last_error:
            lines.append(f"⚠️ {self.last_error}")
        lines.append("Comandos: /rotina aceitar <n> · /rotina off|on · /rotina esquecer (apaga tudo)")
        return "\n".join(lines)


def install(service: Any) -> Routine:
    rot = Routine(service)
    service.routine = rot
    state = {"last": 0.0, "thread": None}

    # sinais: você falou com o agente; o app mandou a posição
    engine_ = service.proactive_engine
    original_activity = engine_.notify_user_activity

    def activity() -> None:
        original_activity()
        try:
            rot.record("atividade")
        except Exception:
            pass
    engine_.notify_user_activity = activity
    local = getattr(service, "local", None)
    if local is not None:
        original_update = local.update_location

        def update(lat: Any, lon: Any, accuracy: Any = None) -> Dict[str, Any]:
            out = original_update(lat, lon, accuracy)
            try:
                rot.record_location(float(lat), float(lon))
            except Exception:
                pass
            return out
        local.update_location = update

    def tick_in_background() -> None:
        if time.time() - state["last"] < 240.0:
            return
        state["last"] = time.time()
        th = state["thread"]
        if th is not None and th.is_alive():
            return
        state["thread"] = threading.Thread(target=rot.tick, daemon=True, name="ByteRotina")
        state["thread"].start()

    service.proactive_engine.background_jobs.append(tick_in_background)
    service.briefing_extras = list(getattr(service, "briefing_extras", [])) + [rot.briefing_lines]

    def command(payload: Dict[str, Any]) -> Dict[str, Any]:
        arg = " ".join(str(payload.get("arg") or "").split()).lower()
        if not arg:
            return {"text": rot.describe()}
        if arg in {"on", "off"}:
            rot.set_enabled(arg == "on")
            return {"text": "Rotina ligada: vou aprender seus horários e sugerir no momento certo." if arg == "on"
                    else "Rotina desligada: não aprendo nem sugiro nada (o que já aprendi fica guardado; /rotina esquecer apaga)."}
        if arg == "esquecer":
            rot.forget()
            return {"text": "Apaguei tudo o que aprendi da sua rotina (lugares, horários e sugestões)."}
        if arg.startswith("aceitar"):
            return {"text": rot.accept(arg.split(" ", 1)[1] if " " in arg else "")}
        return {"text": "Use: /rotina · /rotina aceitar <n> · /rotina off|on · /rotina esquecer"}

    def tool(args: Dict[str, Any], turn: Dict[str, Any]) -> str:
        return rot.describe()

    service.extra_tools["minha_rotina"] = {
        "auto": True, "mission": True, "handler": tool,
        "intent": re.compile(r"\b(rotina|habitos?|costumo|costume|minha semana|meus horarios)\b"),
        "description": "Mostra o que o agente aprendeu da rotina do usuário (casa, horários de saída, horário ativo, repetições).",
        "parameters": {"type": "object", "properties": {}}}
    service.extra_actions.update({"routine_command": command, "routine_tick": lambda p: {"sent": rot.tick()}})
    service.status_providers["rotina"] = lambda: {"enabled": rot.enabled(), "home": bool(rot.home()), "last_error": rot.last_error}
    return rot

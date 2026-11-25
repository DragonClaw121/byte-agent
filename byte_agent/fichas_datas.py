"""Datas da ficha do cliente e alertas comerciais — v13.67 (Customer Success).

Cada ficha guarda:
  datas    renovações, compras e vencimentos: [{"tipo", "data" (AAAA-MM-DD), "descricao", "estimada"}]
  contato  o último contato (e-mail recebido, e-mail preparado, reunião ou nota "liguei/falei/enviei")

Entram por: /data_cliente, /nota_cliente com uma data ("renovação do Figma em 10/03/2027"), a conversa (ferramenta
ficha_registrar_data) e a pesquisa de cliente — inclusive a renovação ESTIMADA de licença anual (compra + 12 meses).

Alertas (no Telegram/painel/app/PC do agente):
  renovação   aos 60, 30, 15, 7 e 1 dia(s) antes (configurável), uma vez por marco;
  sem contato toda segunda (ou no primeiro dia depois), a lista de clientes com compras e sem contato há 45+ dias.
O resumo da manhã mostra as renovações dos próximos 30 dias.
"""
from __future__ import annotations

import json
import re
import time
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

MONTHS = {"janeiro": 1, "fevereiro": 2, "marco": 3, "abril": 4, "maio": 5, "junho": 6, "julho": 7, "agosto": 8,
          "setembro": 9, "outubro": 10, "novembro": 11, "dezembro": 12, "jan": 1, "fev": 2, "mar": 3, "abr": 4, "mai": 5,
          "jun": 6, "jul": 7, "ago": 8, "set": 9, "out": 10, "nov": 11, "dez": 12}
KINDS = ("renovacao", "compra", "vencimento", "outro")
KIND_LABEL = {"renovacao": "renovação", "compra": "compra", "vencimento": "vencimento", "outro": "data"}
CONTACT_EVENTS = {"recebido", "reuniao", "email"}
CONTACT_WORDS = re.compile(r"\b(liguei|ligou|falei|falamos|conversei|conversamos|enviei|mandei|respondi|respondeu|visitei|"
                           r"reuniao|call|whats|whatsapp)\w*\b")
SETTING_ALERTS = "fichas.alertas"
SETTING_SENT = "fichas.alertas_enviados"
DEFAULTS = {"renovacao_dias": [60, 30, 15, 7, 1], "sem_contato_dias": 45}


def fold(text: Any) -> str:
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFKD", str(text or "").lower()) if not unicodedata.combining(c))


def _valid(y: int, m: int, d: int) -> Optional[date]:
    try:
        return date(y, m, d)
    except ValueError:
        return None


def parse_dates(text: str, today: Optional[date] = None) -> List[Tuple[date, int]]:
    """Datas num texto em português: 10/03/2027, 10/03/27, 10/03 (a próxima), 10 de março de 2027, março de 2027,
    03/2027. Devolve [(data, posição)]. Sem ano: a próxima ocorrência a partir de hoje."""
    today = today or date.today()
    low = fold(text)
    found: List[Tuple[date, int]] = []
    taken: List[Tuple[int, int]] = []

    def add(d: Optional[date], m: "re.Match[str]") -> None:
        if any(a <= m.start() < b for a, b in taken):
            return
        taken.append((m.start(), m.end()))                     # mesmo inválida (31/02), a data não vira "02/2027"
        if d is not None:
            found.append((d, m.start()))
    for m in re.finditer(r"\b(\d{1,2})/(\d{1,2})/(\d{2,4})\b", low):
        y = int(m.group(3))
        add(_valid(y + 2000 if y < 100 else y, int(m.group(2)), int(m.group(1))), m)
    month_names = "|".join(sorted(MONTHS, key=len, reverse=True))
    for m in re.finditer(rf"\b(\d{{1,2}})\s+de\s+({month_names})\w*(?:\s+de\s+(\d{{4}}))?", low):
        y = int(m.group(3)) if m.group(3) else today.year
        d = _valid(y, MONTHS[m.group(2)], int(m.group(1)))
        if d is not None and not m.group(3) and d < today:
            d = _valid(y + 1, d.month, d.day)
        add(d, m)
    for m in re.finditer(rf"\b({month_names})\w*\s+(?:de\s+)?(\d{{4}})\b", low):
        add(_valid(int(m.group(2)), MONTHS[m.group(1)], 1), m)
    for m in re.finditer(r"\b(\d{1,2})/(\d{4})\b", low):
        add(_valid(int(m.group(2)), int(m.group(1)), 1), m)
    for m in re.finditer(r"\b(\d{1,2})/(\d{1,2})\b(?!/)", low):
        d = _valid(today.year, int(m.group(2)), int(m.group(1)))
        if d is not None and d < today:
            d = _valid(today.year + 1, d.month, d.day)
        add(d, m)
    return sorted(found, key=lambda x: x[1])


def kind_of(text: str) -> str:
    low = fold(text)
    if re.search(r"\brenova\w*|\bassinatura\b|\banual\b.*\bvence|\bvigencia\b|\bexpira\w*", low):
        return "renovacao"
    if re.search(r"\bvenc\w*|\bprazo\b|\bboleto\b|\bfatura\b", low):
        return "vencimento"
    if re.search(r"\bcompr\w*|\bpedido\b|\bfaturad\w*|\bemiss\w*|\bnota fiscal\b|\bvenda\b", low):
        return "compra"
    return "outro"


def iso(d: date) -> str:
    return d.isoformat()


def human(d: str) -> str:
    try:
        return datetime.strptime(d, "%Y-%m-%d").strftime("%d/%m/%Y")
    except ValueError:
        return d


def days_until(d: str, today: Optional[date] = None) -> Optional[int]:
    try:
        return (datetime.strptime(d, "%Y-%m-%d").date() - (today or date.today())).days
    except ValueError:
        return None


def describe(item: Dict[str, Any], today: Optional[date] = None) -> str:
    left = days_until(item["data"], today)
    when = ("hoje" if left == 0 else f"em {left} dia{'s' if left != 1 else ''}" if left and left > 0
            else f"há {-left} dia{'s' if left != -1 else ''}" if left is not None else "")
    return (f"{KIND_LABEL.get(item['tipo'], 'data')} {item.get('descricao') or ''}".strip()
            + f": {human(item['data'])}" + (f" ({when}" + (", estimada" if item.get("estimada") else "") + ")" if when else ""))


# ---------------- na ficha
def ensure_columns(db: Any) -> None:
    with db.connection() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(client_cards)").fetchall()}
        if "datas" not in cols:
            conn.execute("ALTER TABLE client_cards ADD COLUMN datas TEXT NOT NULL DEFAULT '[]'")
        if "contato" not in cols:
            conn.execute("ALTER TABLE client_cards ADD COLUMN contato REAL NOT NULL DEFAULT 0")


def dates_of(card: Dict[str, Any]) -> List[Dict[str, Any]]:
    raw = card.get("datas")
    if isinstance(raw, list):
        return raw
    try:
        items = json.loads(raw or "[]")
    except ValueError:
        items = []
    return [i for i in items if isinstance(i, dict) and i.get("data")]


def add_date(book: Any, card_n: int, tipo: str, data: str, descricao: str = "", estimada: bool = False) -> Dict[str, Any]:
    """Guarda (ou atualiza) uma data. Mesma descrição e tipo = a mesma coisa (ex.: a renovação do ano seguinte)."""
    tipo = tipo if tipo in KINDS else "outro"
    try:
        when = datetime.strptime(str(data)[:10], "%Y-%m-%d").date()
    except ValueError:
        raise ValueError("Data inválida: use dd/mm/aaaa.")
    if abs((when - date.today()).days) > 365 * 6:
        raise ValueError("Data fora do razoável (mais de 6 anos de distância).")
    descricao = " ".join(str(descricao or "").split())[:120]
    card = book.get(card_n)
    items = [i for i in dates_of(card) if not (i["tipo"] == tipo and fold(i.get("descricao")) == fold(descricao)
                                              and (tipo != "compra" or i["data"] == iso(when)))]
    real_exists = any(i["tipo"] == tipo and fold(i.get("descricao")) == fold(descricao) and not i.get("estimada")
                      for i in dates_of(card))
    if estimada and real_exists:
        return card                                              # a data informada por você vale mais que a estimada
    items.append({"tipo": tipo, "data": iso(when), "descricao": descricao, "estimada": bool(estimada)})
    items.sort(key=lambda i: i["data"])
    with book.db.connection() as conn:
        conn.execute("UPDATE client_cards SET datas = ?, atualizado = ? WHERE n = ?",
                     (json.dumps(items[-30:], ensure_ascii=False), time.time(), int(card_n)))
    return book.get(card_n)


def touch_contact(book: Any, card_n: int, when: Optional[float] = None) -> None:
    with book.db.connection() as conn:
        conn.execute("UPDATE client_cards SET contato = MAX(contato, ?) WHERE n = ?", (when or time.time(), int(card_n)))


def dates_from_note(book: Any, card_n: int, note: str) -> List[Dict[str, Any]]:
    """'/nota_cliente EMS | renovação do Figma em 10/03/2027' -> guarda a renovação."""
    added = []
    for when, pos in parse_dates(note):
        before = note[max(0, pos - 80):pos]                       # o que vem antes da data diz o que ela é
        tipo = kind_of(before) if kind_of(before) != "outro" else kind_of(note)
        desc = re.sub(r"(?i)\b(renova\w*|compr\w*|venc\w*|em|no dia|dia|de|do|da|a|o|vence|expira\w*)\b", " ", before)
        desc = re.sub(r"[\d/]+", " ", desc.split(";")[-1].split(".")[-1])
        desc = " ".join(desc.split()).strip(" ,.;:-")[-60:]
        add_date(book, card_n, tipo, iso(when), desc)
        added.append({"tipo": tipo, "data": iso(when), "descricao": desc})
    return added


def upcoming(book: Any, days: int = 90, kinds: Tuple[str, ...] = ("renovacao", "vencimento"),
             today: Optional[date] = None) -> List[Dict[str, Any]]:
    today = today or date.today()
    out = []
    for card in book.list():
        for item in dates_of(card):
            left = days_until(item["data"], today)
            if item["tipo"] in kinds and left is not None and -3 <= left <= days:
                out.append(dict(item, cliente=card["nome"], n=card["n"], dias=left))
    return sorted(out, key=lambda i: i["dias"])


def last_contact(card: Dict[str, Any]) -> float:
    return float(card.get("contato") or 0)


def stale(book: Any, days: int = 45, now: Optional[float] = None) -> List[Dict[str, Any]]:
    """Clientes com compras/renovações na ficha e sem contato há `days`+ dias (ou nunca, se a ficha tem esse tempo)."""
    now = now or time.time()
    out = []
    for card in book.list():
        if not (card.get("compras") or dates_of(card)):
            continue
        ref = last_contact(card) or float(card.get("criado") or now)
        idle = int((now - ref) // 86400)
        if idle >= days:
            out.append({"cliente": card["nome"], "n": card["n"], "dias": idle, "nunca": not last_contact(card)})
    return sorted(out, key=lambda i: -i["dias"])


def config(db: Any) -> Dict[str, Any]:
    cfg = dict(DEFAULTS)
    saved = db.get_setting(SETTING_ALERTS, {}) or {}
    if isinstance(saved, dict):
        cfg.update({k: v for k, v in saved.items() if k in DEFAULTS})
    return cfg


def card_lines(card: Dict[str, Any], today: Optional[date] = None) -> List[str]:
    lines = [describe(i, today) for i in dates_of(card)
             if i["tipo"] != "compra" or (days_until(i["data"], today) or 0) > -400][-6:]
    if last_contact(card):
        idle = int((time.time() - last_contact(card)) // 86400)
        lines.append(f"último contato: {time.strftime('%d/%m/%Y', time.localtime(last_contact(card)))} "
                     f"({'hoje' if idle == 0 else f'há {idle} dia(s)'})")
    return lines


class Alerts:
    """Roda no agendador (a cada 3 h): renovação perto (um aviso por marco) e, 1x por semana, quem está sem contato."""

    def __init__(self, service: Any, clock: Any = time.time):
        self.service, self.db, self.clock = service, service.db, clock

    def _sent(self) -> Dict[str, float]:
        data = self.db.get_setting(SETTING_SENT, {}) or {}
        return data if isinstance(data, dict) else {}

    def tick(self) -> List[str]:
        book = getattr(self.service, "client_book", None)
        if book is None:
            return []
        now = self.clock()
        today = datetime.fromtimestamp(now).date()
        cfg = config(self.db)
        marks = sorted({int(x) for x in cfg["renovacao_dias"] if int(x) >= 0}, reverse=True)
        sent, out, actions = self._sent(), [], []
        for item in upcoming(book, max(marks or [0]), today=today):
            left = item["dias"]
            if left < 0:
                continue
            crossed = [m for m in marks if left <= m]
            if not crossed:
                continue
            mark = min(crossed)
            key = f"{item['n']}|{item['tipo']}|{fold(item['descricao'])}|{item['data']}|{mark}"
            older = [k for k in sent if k.startswith(f"{item['n']}|{item['tipo']}|{fold(item['descricao'])}|{item['data']}|")
                     and int(k.rsplit("|", 1)[1]) <= mark]
            if key in sent or older:
                continue
            sent[key] = now
            when = "hoje" if left == 0 else "amanhã" if left == 1 else f"em {left} dias"
            if len(actions) < 3:                                  # v13.68: um toque prepara o e-mail de renovação
                actions.append({"label": f"✉️ Renovação — {item['cliente']}"[:40],
                                "data": f"cs:ren:{item['n']}:{item['data'].replace('-', '')}"})
            out.append(f"🔁 {item['cliente']}: {KIND_LABEL[item['tipo']]}"
                       + (f" de {item['descricao']}" if item["descricao"] else "") + f" {when} ({human(item['data'])}"
                       + (", data estimada" if item.get("estimada") else "") + ").")
        last_week = float(self.db.get_setting("fichas.sem_contato_em", 0) or 0)
        if today.weekday() == 0 or now - last_week > 8 * 86400:
            if now - last_week > 6 * 86400:
                idle = stale(book, int(cfg["sem_contato_dias"]), now)
                self.db.set_setting("fichas.sem_contato_em", now)
                if idle:
                    for i in idle[:3 - min(3, len(actions))]:
                        actions.append({"label": f"✉️ Follow-up — {i['cliente']}"[:40], "data": f"cs:fup:{i['n']}"})
                    lines = [f"• {i['cliente']} — " + (f"{i['dias']} dias sem contato" if not i["nunca"]
                                                     else f"sem contato registrado há {i['dias']} dias") for i in idle[:8]]
                    out.append(f"📇 Clientes sem contato há {cfg['sem_contato_dias']}+ dias:\n" + "\n".join(lines)
                               + ("\n…e mais." if len(idle) > 8 else "")
                               + "\nToque num botão abaixo (ou peça \"e-mail de follow-up para <cliente>\") que eu preparo.")
        cutoff = now - 400 * 86400
        self.db.set_setting(SETTING_SENT, {k: v for k, v in sent.items() if v > cutoff})
        if out:
            text = "\n\n".join(out)
            if any(m.startswith("🔁") for m in out):
                text += "\n\nQuer que eu prepare o e-mail de renovação? Toque no botão (ele só prepara: o envio é seu)."
            self.db.enqueue_proactive_message("clientes", text, brain_used="CLIENTES", actions=actions)
        return out

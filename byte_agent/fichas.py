"""Ficha de cliente — v13.60.

Antes, cada função sabia do cliente só no momento: a pesquisa gerava um plano e esquecia, o e-mail lia a conversa
das últimas horas, e na semana seguinte o Bit começava do zero. Agora cada cliente tem uma ficha persistente com:
  nomes (razão social, fantasia, como você chama), CNPJs (inclusive de outras unidades), pessoas-chave, compras e
  dados internos (acumulados, sem repetir), resumo, próximos passos, o último plano de conta e o histórico
  (pesquisas, e-mails preparados, telas lidas, notas suas).

Quem escreve: a pesquisa de cliente, o Bit no PC (telas lidas e e-mails preparados) e você (/nota_cliente).
Quem lê: a pesquisa (o histórico entra no plano novo), o Preparar e-mail e o Olhar a tela do PC, e a conversa em
qualquer canal (quando a mensagem cita um cliente que tem ficha).

Os dados ficam no banco do próprio agente (cada convidado tem o seu). Nada sai daqui além do que já ia para a IA.
"""
from __future__ import annotations

import json
import re
import time
import unicodedata
from typing import Any, Dict, List, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS client_cards (
    n INTEGER PRIMARY KEY AUTOINCREMENT,
    nome TEXT NOT NULL,
    chave TEXT NOT NULL UNIQUE,
    apelidos TEXT NOT NULL DEFAULT '[]',
    cnpjs TEXT NOT NULL DEFAULT '[]',
    contatos TEXT NOT NULL DEFAULT '[]',
    compras TEXT NOT NULL DEFAULT '',
    resumo TEXT NOT NULL DEFAULT '',
    proximos TEXT NOT NULL DEFAULT '[]',
    plano TEXT NOT NULL DEFAULT '',
    criado REAL NOT NULL,
    atualizado REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS client_events (
    n INTEGER PRIMARY KEY AUTOINCREMENT,
    card INTEGER NOT NULL,
    tipo TEXT NOT NULL,
    texto TEXT NOT NULL,
    t REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS client_events_card ON client_events(card, t);
"""
SUFFIXES = set("""ltda ltd s/a sa s.a. s.a me epp eireli cia companhia sociedade grupo holding inc corp limitada
industria comercio servicos""".split())
EVENT_ICONS = {"pesquisa": "🏢", "email": "✉️", "tela": "📸", "nota": "📝", "reuniao": "🎙️", "recebido": "📥"}
COMPRAS_MAX = 5000


def fold(text: Any) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", str(text or "").lower()) if not unicodedata.combining(c))


def name_key(name: str) -> str:
    """'EMS S/A' -> 'ems'; 'Rio Biofarma Brasil Ltda.' -> 'rio biofarma brasil'."""
    words = re.findall(r"[a-z0-9&]+(?:/[a-z]+)?", fold(name))
    kept = [w for w in words if w not in SUFFIXES and w.replace("/", "") not in SUFFIXES]
    return " ".join(kept or words)[:120]


def cnpj_digits(text: Any) -> str:
    return re.sub(r"\D", "", str(text or ""))


def merge_lines(old: str, new: str, limit: int = COMPRAS_MAX) -> str:
    """Junta textos de compras sem repetir linhas; o mais novo por último; corta o mais velho se passar do limite."""
    seen, out = set(), []
    for line in (str(old or "") + "\n" + str(new or "")).split("\n"):
        line = line.strip()
        key = fold(re.sub(r"\s+", " ", line))
        if line and key not in seen:
            seen.add(key)
            out.append(line)
    text = "\n".join(out)
    return text[-limit:] if len(text) > limit else text


class ClientBook:
    def __init__(self, db: Any):
        self.db = db
        with db.connection() as conn:
            conn.executescript(SCHEMA)

    # ---------------- leitura
    @staticmethod
    def _row(r: Any) -> Dict[str, Any]:
        d = dict(r)
        for k in ("apelidos", "cnpjs", "contatos", "proximos"):
            try:
                d[k] = json.loads(d.get(k) or "[]")
            except ValueError:
                d[k] = []
        return d

    def list(self) -> List[Dict[str, Any]]:
        with self.db.connection() as conn:
            rows = conn.execute("SELECT * FROM client_cards ORDER BY atualizado DESC").fetchall()
        return [self._row(r) for r in rows]

    def get(self, ref: Any) -> Dict[str, Any]:
        """Pelo número da ficha, pelo CNPJ ou pelo nome."""
        raw = str(ref or "").strip().lstrip("#")
        if raw.isdigit() and len(raw) < 6:
            with self.db.connection() as conn:
                row = conn.execute("SELECT * FROM client_cards WHERE n = ?", (int(raw),)).fetchone()
            if row is not None:
                return self._row(row)
        card = self.find(raw)
        if card is None:
            raise ValueError(f"Não há ficha de \"{raw}\". Veja /clientes (as fichas nascem na pesquisa de cliente ou com "
                             "/nota_cliente <nome> | <nota>).")
        return card

    def events(self, card_n: int, limit: int = 10) -> List[Dict[str, Any]]:
        with self.db.connection() as conn:
            rows = conn.execute("SELECT * FROM client_events WHERE card = ? ORDER BY t DESC, n DESC LIMIT ?",
                                (int(card_n), int(limit))).fetchall()
        return [dict(r) for r in rows]

    def find(self, text: str) -> Optional[Dict[str, Any]]:
        """A ficha citada no texto: pelo CNPJ (mesma raiz = mesmo grupo) ou pelo nome/apelido como palavra inteira.
        Com vários, o nome mais longo (mais específico) ganha."""
        if not str(text or "").strip():
            return None
        cards = self.list()
        digits = {cnpj_digits(m) for m in re.findall(r"\d{2}\.?\d{3}\.?\d{3}/?\d{4}-?\d{2}", str(text))}
        roots = {d[:8] for d in digits if len(d) == 14}
        if roots:
            for c in cards:
                if any(cnpj_digits(x)[:8] in roots for x in c["cnpjs"]):
                    return c
        low = " " + " ".join(re.findall(r"[a-z0-9&]+", fold(text))) + " "
        best, size = None, 0
        for c in cards:
            for name in [c["nome"], c["chave"]] + list(c["apelidos"]):
                key = name_key(name)
                if len(key) < 3:
                    continue
                if f" {key} " in low and len(key) > size:
                    best, size = c, len(key)
        return best

    # ---------------- escrita
    def upsert(self, nome: str, cnpj: str = "", apelidos: Optional[List[str]] = None, resumo: Optional[str] = None,
               compras: str = "", contatos: Optional[List[str]] = None, proximos: Optional[List[str]] = None,
               plano: Optional[str] = None) -> Dict[str, Any]:
        nome = " ".join(str(nome or "").split())[:160]
        if not nome and not cnpj:
            raise ValueError("Diga o nome ou o CNPJ do cliente.")
        now = time.time()
        card = self.find(" ".join(filter(None, [cnpj, nome] + list(apelidos or []))))
        names = [x for x in [nome] + list(apelidos or []) if x and x.strip()]
        if card is None:
            key = name_key(nome or cnpj)
            with self.db.connection() as conn:
                conn.execute("INSERT OR IGNORE INTO client_cards (nome, chave, criado, atualizado) VALUES (?, ?, ?, ?)",
                             (nome or cnpj, key, now, now))
                n = conn.execute("SELECT n FROM client_cards WHERE chave = ?", (key,)).fetchone()[0]
            card = self.get(n)
        aliases = list(card["apelidos"])
        for x in names:
            x = " ".join(x.split())[:160]
            if fold(x) not in {fold(a) for a in aliases} and fold(x) != fold(card["nome"]):
                aliases.append(x)
        cnpjs = list(card["cnpjs"])
        if cnpj and cnpj_digits(cnpj) not in {cnpj_digits(c) for c in cnpjs}:
            cnpjs.append(cnpj)
        people = list(card["contatos"])
        for p in contatos or []:
            p = " ".join(str(p).split())[:200]
            if p and fold(p) not in {fold(x) for x in people}:
                people.append(p)
        fields = {"apelidos": json.dumps(aliases[-12:], ensure_ascii=False), "cnpjs": json.dumps(cnpjs[-10:]),
                  "contatos": json.dumps(people[-20:], ensure_ascii=False),
                  "compras": merge_lines(card["compras"], compras) if compras else card["compras"], "atualizado": now}
        if resumo:
            fields["resumo"] = str(resumo)[:2000]
        if proximos:
            fields["proximos"] = json.dumps([str(p)[:300] for p in proximos][:8], ensure_ascii=False)
        if plano:
            fields["plano"] = str(plano)[:200]
        sets = ", ".join(f"{k} = ?" for k in fields)
        with self.db.connection() as conn:
            conn.execute(f"UPDATE client_cards SET {sets} WHERE n = ?", list(fields.values()) + [card["n"]])
        return self.get(card["n"])

    def add_event(self, card_n: int, tipo: str, texto: str, min_gap_s: float = 0.0) -> bool:
        """Registra no histórico. min_gap_s evita repetir o mesmo tipo em seguida (ex.: telas lidas)."""
        now = time.time()
        if min_gap_s:
            last = self.events(card_n, 1)
            if last and last[0]["tipo"] == tipo and now - float(last[0]["t"]) < min_gap_s:
                return False
        with self.db.connection() as conn:
            conn.execute("INSERT INTO client_events (card, tipo, texto, t) VALUES (?, ?, ?, ?)",
                         (int(card_n), tipo, " ".join(str(texto).split())[:600], now))
            conn.execute("UPDATE client_cards SET atualizado = ? WHERE n = ?", (now, int(card_n)))
        return True

    def delete(self, ref: Any) -> Dict[str, Any]:
        card = self.get(ref)
        with self.db.connection() as conn:
            conn.execute("DELETE FROM client_events WHERE card = ?", (card["n"],))
            conn.execute("DELETE FROM client_cards WHERE n = ?", (card["n"],))
        return card

    # ---------------- para os prompts e para você
    def context_block(self, card: Dict[str, Any], limit: int = 3000) -> str:
        lines = [f"FICHA DO CLIENTE (dados guardados de conversas e pesquisas anteriores): {card['nome']}"]
        if card["apelidos"]:
            lines.append("Também chamado de: " + ", ".join(card["apelidos"]))
        if card["cnpjs"]:
            lines.append("CNPJs: " + ", ".join(card["cnpjs"]))
        if card["resumo"]:
            lines.append("Resumo: " + card["resumo"])
        if card["contatos"]:
            lines.append("Pessoas-chave: " + "; ".join(card["contatos"]))
        if card["compras"]:
            lines.append("Compras e dados internos:\n" + card["compras"][-1500:])
        if card["proximos"]:
            lines.append("Próximos passos combinados: " + "; ".join(card["proximos"]))
        ev = self.events(card["n"], 6)
        if ev:
            lines.append("Histórico recente: " + " | ".join(
                f"{time.strftime('%d/%m', time.localtime(float(e['t'])))} {e['tipo']}: {e['texto'][:160]}" for e in ev))
        return "\n".join(lines)[:limit]

    def format_card(self, card: Dict[str, Any]) -> str:
        lines = [f"🗂️ Ficha #{card['n']} — {card['nome']}"]
        if card["cnpjs"]:
            lines.append("CNPJ: " + ", ".join(card["cnpjs"]))
        if card["apelidos"]:
            lines.append("Também: " + ", ".join(card["apelidos"]))
        if card["resumo"]:
            lines += ["", card["resumo"]]
        if card["contatos"]:
            lines += ["", "👥 Pessoas-chave:"] + [f"• {c}" for c in card["contatos"]]
        if card["compras"]:
            lines += ["", "🧾 Compras e dados internos:", card["compras"][-1800:]]
        if card["proximos"]:
            lines += ["", "➡️ Próximos passos:"] + [f"• {p}" for p in card["proximos"]]
        if card["plano"]:
            lines += ["", f"📄 Último plano de conta: {card['plano']}"]
        ev = self.events(card["n"], 8)
        if ev:
            lines += ["", "🕓 Histórico:"] + [
                f"{EVENT_ICONS.get(e['tipo'], '•')} {time.strftime('%d/%m %H:%M', time.localtime(float(e['t'])))} {e['texto'][:200]}"
                for e in ev]
        return "\n".join(lines)


def format_list(cards: List[Dict[str, Any]]) -> str:
    if not cards:
        return ("Nenhuma ficha de cliente ainda. Elas nascem sozinhas na pesquisa de cliente (/cliente ou \"pesquise esse "
                "cliente\" no Bit do PC) ou com /nota_cliente <nome> | <nota>.")
    lines = ["🗂️ Fichas de clientes (mais recentes primeiro):"]
    for c in cards[:40]:
        when = time.strftime("%d/%m", time.localtime(float(c["atualizado"])))
        lines.append(f"{c['n']}. {c['nome']}" + (f" — {c['cnpjs'][0]}" if c["cnpjs"] else "") + f" · {when}")
    lines.append("\n/ficha <número ou nome> abre · /nota_cliente <nome> | <nota> anota")
    return "\n".join(lines)


def install(service: Any) -> ClientBook:
    book = ClientBook(service.db)
    service.client_book = book

    def hook(message: str, turn: Dict[str, Any]) -> Optional[str]:
        """Qualquer canal: a mensagem cita um cliente com ficha -> a ficha entra no prompt."""
        card = book.find(message)
        if card is None:
            return None
        turn.setdefault("client_card", card["n"])
        return book.context_block(card) + "\n(Use esses dados; o que não estiver aí, não invente.)"
    service.pre_turn_hooks.append(hook)

    def act_list(p: Dict[str, Any]) -> Dict[str, Any]:
        cards = book.list()
        return {"fichas": [{k: c[k] for k in ("n", "nome", "cnpjs", "atualizado")} for c in cards], "text": format_list(cards)}

    def act_get(p: Dict[str, Any]) -> Dict[str, Any]:
        card = book.get(p.get("ref") or p.get("n") or p.get("nome") or "")
        return {"ficha": card, "eventos": book.events(card["n"], 20), "text": book.format_card(card)}

    def act_note(p: Dict[str, Any]) -> Dict[str, Any]:
        who, note = str(p.get("cliente") or "").strip(), str(p.get("nota") or "").strip()
        if not who or not note:
            raise ValueError("Use: /nota_cliente <nome ou CNPJ> | <nota> (ex.: /nota_cliente EMS | renovação do Figma em março)")
        card = book.upsert(who, cnpj=who if len(cnpj_digits(who)) == 14 else "")
        book.add_event(card["n"], "nota", note)
        if re.search(r"(?i)\b(compr|pedido|licen|renova|valor|r\$)", note):
            card = book.upsert(card["nome"], compras=f"[{time.strftime('%d/%m/%Y')}] {note}")
        return {"ficha": card, "text": f"📝 Anotado na ficha #{card['n']} — {card['nome']}."}

    def act_delete(p: Dict[str, Any]) -> Dict[str, Any]:
        card = book.delete(p.get("ref") or "")
        return {"text": f"🗑️ Ficha de {card['nome']} apagada (com o histórico)."}

    service.extra_actions.update({"client_cards": act_list, "client_card": act_get, "client_note": act_note,
                                  "client_card_delete": act_delete})

    def tool(args: Dict[str, Any], turn: Dict[str, Any]) -> str:
        try:
            card = book.get(str(args.get("cliente") or ""))
        except ValueError as err:
            return f"ERRO: {err}"
        return book.context_block(card, limit=5000)

    service.extra_tools["ficha_cliente"] = {
        "auto": True, "taints": True, "handler": tool,
        "intent": re.compile(r"\b(ficha|cliente|clientes|cnpj|historico (do|da) (cliente|empresa))\b"),
        "description": "Lê a ficha guardada de um cliente (CNPJs, pessoas, compras, resumo, próximos passos e histórico de "
                       "pesquisas, e-mails e notas). Use antes de responder sobre um cliente que o usuário já atendeu.",
        "parameters": {"type": "object", "required": ["cliente"],
                       "properties": {"cliente": {"type": "string", "description": "Nome, número da ficha ou CNPJ"}}}}
    return book

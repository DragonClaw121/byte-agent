"""Modelos-base de e-mail do usuário — v13.51.

O Bit passa a escrever COMO o usuário escreve: cada modelo tem nome, situação de uso ("follow-up de orçamento",
"cobrança", "reativação"), assunto e texto, com campos opcionais {{nome}}, {{pedido}}... Sempre que o Bit prepara um
e-mail (Bit no PC, triagem da caixa, respostas na conversa, e-mail de abordagem do plano de cliente), ele:
  1. escolhe o modelo que combina com a situação (pelo nome/uso; com vários parecidos, a LLM decide);
  2. mantém o texto, a estrutura e o tom do modelo, preenchendo só o que muda; o que faltar vira [PREENCHER: ...];
  3. sem modelo adequado, escreve no ESTILO do usuário (saudações e despedidas tiradas dos modelos) e com a assinatura.
Entrada dos modelos: painel (aba Gmail & Outlook), app/Telegram (/modelo_email), Bit no PC (colar, "salvar como
modelo" ou importar os .oft e a assinatura do Outlook).
"""
from __future__ import annotations

import re
import time
import unicodedata
from typing import Any, Callable, Dict, List, Optional, Tuple

SCHEMA = """
CREATE TABLE IF NOT EXISTS email_templates (
    n INTEGER PRIMARY KEY AUTOINCREMENT,
    nome TEXT NOT NULL UNIQUE,
    uso TEXT NOT NULL DEFAULT '',
    assunto TEXT NOT NULL DEFAULT '',
    corpo TEXT NOT NULL,
    origem TEXT NOT NULL DEFAULT 'manual',
    usos INTEGER NOT NULL DEFAULT 0,
    criado REAL NOT NULL
);
"""
SETTING_SIGNATURE = "email.assinatura"
MAX_TEMPLATES = 200
STOP = set("""de da do das dos e o a os as um uma para por com sem em no na nos nas ao que se sobre meu minha seu sua email
e-mail mail responder resposta escrever escreva redigir redija mande enviar envie pra pro favor""".split())


def fold(text: Any) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", str(text or "").lower()) if not unicodedata.combining(c))


def words(text: Any) -> set:
    return {w for w in re.split(r"[^\w]+", fold(text)) if len(w) >= 3 and w not in STOP}


def fields_of(text: str) -> List[str]:
    out: List[str] = []
    for f in re.findall(r"\{\{\s*([\w .-]{1,40})\s*\}\}", str(text or "")):
        f = f.strip()
        if f not in out:
            out.append(f)
    return out


class EmailTemplates:
    def __init__(self, db: Any):
        self.db = db
        with db.connection() as conn:
            conn.executescript(SCHEMA)

    # ---------------- cadastro
    def list(self) -> List[Dict[str, Any]]:
        with self.db.connection() as conn:
            rows = conn.execute("SELECT * FROM email_templates ORDER BY usos DESC, nome").fetchall()
        return [dict(r, campos=fields_of(r["assunto"] + "\n" + r["corpo"])) for r in rows]

    def get(self, n: Any) -> Dict[str, Any]:
        try:
            num = int(str(n).strip().lstrip("#"))
        except ValueError:
            raise ValueError("Use o número do modelo (veja /modelos_email).")
        with self.db.connection() as conn:
            row = conn.execute("SELECT * FROM email_templates WHERE n = ?", (num,)).fetchone()
        if row is None:
            raise ValueError(f"Não há modelo #{num}.")
        return dict(row, campos=fields_of(row["assunto"] + "\n" + row["corpo"]))

    def save(self, nome: str, corpo: str, uso: str = "", assunto: str = "", origem: str = "manual") -> Dict[str, Any]:
        nome = " ".join(str(nome or "").split())[:80]
        corpo = str(corpo or "").replace("\r\n", "\n").strip()[:8000]
        if not nome:
            raise ValueError("Dê um nome ao modelo (ex.: Follow-up de orçamento).")
        if len(corpo) < 10:
            raise ValueError("O texto do modelo está vazio ou curto demais.")
        uso, assunto = " ".join(str(uso or "").split())[:200], " ".join(str(assunto or "").split())[:200]
        with self.db.connection() as conn:
            count = conn.execute("SELECT COUNT(*) FROM email_templates").fetchone()[0]
            exists = conn.execute("SELECT n FROM email_templates WHERE nome = ?", (nome,)).fetchone()
            if exists is None and count >= MAX_TEMPLATES:
                raise ValueError(f"Limite de {MAX_TEMPLATES} modelos: apague algum antes.")
            conn.execute("INSERT INTO email_templates (nome, uso, assunto, corpo, origem, criado) VALUES (?, ?, ?, ?, ?, ?) "
                         "ON CONFLICT(nome) DO UPDATE SET uso = excluded.uso, assunto = excluded.assunto, "
                         "corpo = excluded.corpo, origem = excluded.origem", (nome, uso, assunto, corpo, origem, time.time()))
            n = conn.execute("SELECT n FROM email_templates WHERE nome = ?", (nome,)).fetchone()[0]
        self.db.record_audit("email_template_saved", "modelos_email", "success", {"origem": origem})
        return self.get(n)

    def delete(self, n: Any) -> Dict[str, Any]:
        tpl = self.get(n)
        with self.db.connection() as conn:
            conn.execute("DELETE FROM email_templates WHERE n = ?", (tpl["n"],))
        return tpl

    def mark_used(self, n: int) -> None:
        with self.db.connection() as conn:
            conn.execute("UPDATE email_templates SET usos = usos + 1 WHERE n = ?", (int(n),))

    def signature(self) -> str:
        return str(self.db.get_setting(SETTING_SIGNATURE, "") or "").strip()

    def set_signature(self, text: str) -> str:
        text = str(text or "").replace("\r\n", "\n").strip()[:1500]
        self.db.set_setting(SETTING_SIGNATURE, text)
        return text

    # ---------------- escolher e orientar
    def choose(self, situation: str, llm: Optional[Callable[[str, str], str]] = None) -> Optional[Dict[str, Any]]:
        """O modelo que combina com a situação. Sem nenhum parecido, None (aí vale só o estilo)."""
        items = self.list()
        if not items:
            return None
        want = words(situation)
        scored: List[Tuple[float, Dict[str, Any]]] = []
        for t in items:
            key = words(f"{t['nome']} {t['uso']}")
            subject = words(t["assunto"])
            score = 2.0 * len(want & key) + 0.5 * len(want & subject)
            if score:
                scored.append((score, t))
        scored.sort(key=lambda x: -x[0])
        if llm is not None and len(items) > 1 and (not scored or len(scored) > 1):
            menu = "\n".join(f"{t['n']}: {t['nome']} — {t['uso'] or 'sem descrição'}" for t in items[:40])
            try:
                raw = llm("Escolha o modelo de e-mail do usuário que serve para a situação. Responda SÓ o número do modelo, ou 0 "
                          "se nenhum servir de verdade.", f"SITUAÇÃO: {situation[:1500]}\n\nMODELOS:\n{menu}")
                num = int(re.findall(r"\d+", str(raw or "0"))[0]) if re.findall(r"\d+", str(raw or "")) else -1
                if num == 0:
                    return None
                match = next((t for t in items if t["n"] == num), None)
                if match is not None:
                    return match
            except Exception:
                pass
        return scored[0][1] if scored and scored[0][0] >= 2.0 else None

    def style(self) -> str:
        """Saudações e despedidas que o usuário usa (tiradas dos próprios modelos, sem custo de LLM)."""
        greet, close = [], []
        for t in self.list()[:30]:
            lines = [ln.strip() for ln in t["corpo"].split("\n") if ln.strip()]
            if lines:
                if len(lines[0]) <= 60 and lines[0] not in greet:
                    greet.append(lines[0])
                tail = [ln for ln in lines[-4:] if len(ln) <= 40 and not ln.startswith("{{")]
                if tail and tail[0] not in close:
                    close.append(tail[0])
        out = []
        if greet:
            out.append("saudações que ele costuma usar: " + " | ".join(greet[:5]))
        if close:
            out.append("despedidas: " + " | ".join(close[:5]))
        return "; ".join(out)

    def guidance(self, situation: str, llm: Optional[Callable[[str, str], str]] = None) -> Tuple[Optional[Dict[str, Any]], str]:
        """(modelo escolhido, bloco para o prompt de quem escreve o e-mail)."""
        tpl = self.choose(situation, llm)
        sig = self.signature()
        parts: List[str] = []
        if tpl is not None:
            parts.append(
                f"MODELO DO USUÁRIO A SEGUIR (\"{tpl['nome']}\"): use-o como base. Mantenha o texto, a estrutura, as expressões "
                "e o tom dele; preencha os {{campos}} e os trechos que variam com os dados da situação; mude só o que a "
                "situação exige. Dado que não existir, escreva [PREENCHER: o que falta] (não invente)."
                f"\n<modelo>\nAssunto: {tpl['assunto'] or '(defina)'}\n{tpl['corpo']}\n</modelo>")
        else:
            st = self.style()
            if st:
                parts.append(f"ESTILO DO USUÁRIO (escreva como ele): {st}.")
        if sig:
            parts.append(f"ASSINATURA DO USUÁRIO (termine o e-mail exatamente com ela, sem [SEU NOME]):\n{sig}")
        return tpl, "\n\n".join(parts)

    def finish(self, body: str, tpl: Optional[Dict[str, Any]] = None) -> str:
        """Garante a assinatura no fim e conta o uso do modelo."""
        body = str(body or "").strip()
        sig = self.signature()
        if sig:
            body = body.replace("[SEU NOME]", sig.split("\n")[0])
            if sig.split("\n")[0] not in body:
                body = f"{body}\n\n{sig}"
        if tpl is not None:
            self.mark_used(tpl["n"])
        return body


def format_list(items: List[Dict[str, Any]], signature: str) -> str:
    if not items:
        lines = ["✉️ Nenhum modelo de e-mail ainda.",
                 "Salve um: /modelo_email Follow-up de orçamento | depois de enviar uma proposta",
                 "(na linha de baixo, o texto do modelo; use {{nome}}, {{pedido}}... onde muda)"]
    else:
        lines = ["✉️ Seus modelos de e-mail (o Bit usa o que combina com cada situação):"]
        for t in items:
            lines.append(f"{t['n']}. {t['nome']}" + (f" — {t['uso']}" if t["uso"] else "")
                         + (f" · campos: {', '.join(t['campos'])}" if t["campos"] else "") + f" · usado {t['usos']}x")
        lines.append("Ver: /modelo_email ver <n> · apagar: /modelo_email apagar <n>")
    lines.append("Assinatura: " + (signature.replace("\n", " / ") if signature else "não definida (/assinatura <texto>)"))
    return "\n".join(lines)


def parse_command(arg: str) -> Dict[str, str]:
    """'/modelo_email Nome | uso\\nAssunto: ...\\ntexto' -> campos. A primeira linha é nome | uso."""
    head, _, body = str(arg or "").partition("\n")
    nome, _, uso = head.partition("|")
    body = body.strip()
    subject = ""
    m = re.match(r"(?i)^assunto\s*:\s*(.+)$", body.split("\n", 1)[0].strip()) if body else None
    if m:
        subject = m.group(1).strip()
        body = body.split("\n", 1)[1].strip() if "\n" in body else ""
    return {"nome": nome.strip(), "uso": uso.strip(), "assunto": subject, "corpo": body}


def install(service: Any) -> EmailTemplates:
    templates = EmailTemplates(service.db)
    service.email_templates = templates

    def hook(message: str, turn: Dict[str, Any]) -> Optional[str]:
        """Na conversa, quando o pedido é escrever/responder um e-mail, o modelo e a assinatura entram no prompt."""
        if not re.search(r"\b(e-?mails?|responde\w*|redig\w*|escrev\w*.{0,20}(mensagem|e-?mail))\b", fold(message)):
            return None
        if not templates.list() and not templates.signature():
            return None
        tpl, block = templates.guidance(message)
        if tpl is not None:
            turn.setdefault("email_template", tpl["n"])
        return block or None

    service.pre_turn_hooks.append(hook)
    service.extra_actions.update({
        "email_templates_list": lambda p: {"modelos": templates.list(), "assinatura": templates.signature(),
                                           "text": format_list(templates.list(), templates.signature())},
        "email_template_save": lambda p: {"modelo": templates.save(str(p.get("nome") or ""), str(p.get("corpo") or ""),
                                                                   str(p.get("uso") or ""), str(p.get("assunto") or ""),
                                                                   str(p.get("origem") or "manual")),
                                          "modelos": templates.list()},
        "email_template_delete": lambda p: {"apagado": templates.delete(p.get("n")), "modelos": templates.list()},
        "email_signature": lambda p: {"assinatura": templates.set_signature(str(p.get("assinatura") or ""))
                                      if "assinatura" in p else templates.signature()},
    })
    return templates

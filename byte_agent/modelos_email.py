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
SETTING_SIGNATURE_HTML = "email.assinatura_html"
MAX_HTML = 1_500_000                       # com as imagens (logo, banner) embutidas em base64
LLM_HTML_LIMIT = 24_000                    # acima disso, só os {{campos}} são trocados (o HTML não vai para a LLM)
MAX_TEMPLATES = 200
STOP = set("""de da do das dos e o a os as um uma para por com sem em no na nos nas ao que se sobre meu minha seu sua email
e-mail mail responder resposta escrever escreva redigir redija mande enviar envie pra pro favor""".split())


def fold(text: Any) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", str(text or "").lower()) if not unicodedata.combining(c))


def words(text: Any) -> set:
    return {w for w in re.split(r"[^\w]+", fold(text)) if len(w) >= 3 and w not in STOP}


# campos do modelo: {{nome}} ou, como muita gente escreve no Outlook, [Nome] / [SOFTWARE] (começando por maiúscula)
FIELD_RE = re.compile(r"\{\{\s*([\w .-]{1,40})\s*\}\}|\[([A-ZÀ-Ý][\wÀ-ÿ .-]{0,30})\]")
NOT_FIELDS = {"SEU NOME", "PREENCHER"}
NO_MODEL = re.compile(r"\b(sem|nenhum|nao use|nao usa|nao usar|sem usar|fora do)\b.{0,15}\bmodelo")
MODEL_REF = re.compile(r"\b(modelos?|templates?|padrao|meu formato|formato que (eu )?(ja )?uso)\b")


def fields_of(text: str) -> List[str]:
    out: List[str] = []
    for m in FIELD_RE.finditer(str(text or "")):
        f = (m.group(1) or m.group(2)).strip()
        if f and f not in out and f.upper() not in NOT_FIELDS:
            out.append(f)
    return out


def clean_html(html: str) -> str:
    """Tira o "peso" do HTML do Outlook/Word (comentários condicionais, <o:p>, mso-*, metadados), mantendo tabelas,
    estilos, cores e imagens."""
    text = str(html or "")
    text = re.sub(r"(?is)<!--\[if.*?<!\[endif\]-->", "", text)
    text = re.sub(r"(?s)<!--(?!\[if).*?-->", "", text)
    text = re.sub(r"(?is)<(script|xml|iframe|object|embed)[^>]*>.*?</\1>|<(script|iframe|object|embed)[^>]*/?>", "", text)
    text = re.sub(r"""(?i)\son[a-z]+\s*=\s*("[^"]*"|'[^']*'|[^\s>]+)""", "", text)        # nada de código no e-mail
    text = re.sub(r"(?i)(href|src)\s*=\s*([\"']?)\s*javascript:[^\"'\s>]*", r"\1=\2#", text)
    text = re.sub(r"(?is)</?o:p>|<\?xml[^>]*>|<meta[^>]*>|<link[^>]*>", "", text)
    text = re.sub(r"(?i)\s*mso-[\w-]+\s*:\s*[^;\"']+;?", "", text)
    text = re.sub(r">\s+<", "> <", text).strip()
    if len(text) > MAX_HTML:
        raise ValueError("O modelo em HTML ficou grande demais (imagens muito pesadas). Diminua as imagens e tente de novo.")
    return text


def hide_images(html: str) -> Tuple[str, List[str]]:
    """Troca as imagens embutidas (data:...) por marcadores curtos — a LLM não precisa (nem deve) ver o base64."""
    found: List[str] = []

    def repl(m: "re.Match[str]") -> str:
        found.append(m.group(0))
        return f"bit-img:{len(found) - 1}"
    return re.sub(r"data:image/[\w.+-]+;base64,[A-Za-z0-9+/=\s]+", repl, str(html or "")), found


def show_images(html: str, found: List[str]) -> str:
    return re.sub(r"bit-img:(\d+)", lambda m: found[int(m.group(1))] if int(m.group(1)) < len(found) else m.group(0), html)


def html_text(html: str) -> str:
    import html as html_lib
    text = re.sub(r"(?is)<(style|script|head|title)[^>]*>.*?</\1>", " ", str(html or ""))
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>|</li>|</h\d>", "\n", text)
    text = html_lib.unescape(re.sub(r"<[^>]+>", "", text)).replace("\xa0", " ")
    lines = [" ".join(line.split()) for line in text.split("\n")]
    out: List[str] = []
    for line in lines:
        if line or (out and out[-1]):
            out.append(line)
    return "\n".join(out).strip()


def fill_fields(text: str, values: Dict[str, Any], html: bool = False) -> str:
    import html as html_lib

    folded = {fold(k): v for k, v in (values or {}).items()}

    def repl(m: "re.Match[str]") -> str:
        key = (m.group(1) or m.group(2)).strip()
        if key.upper() in NOT_FIELDS:
            return m.group(0)
        value = values.get(key) if key in (values or {}) else folded.get(fold(key))
        if value in (None, ""):
            return f"[PREENCHER: {html_lib.escape(key)}]" if html and m.group(1) else m.group(0)
        value = str(value)
        return html_lib.escape(value).replace("\n", "<br>") if html else value
    return FIELD_RE.sub(repl, str(text or ""))


def tag_count(html: str) -> int:
    return len(re.findall(r"<[a-zA-Z]", str(html or "")))


class EmailTemplates:
    def __init__(self, db: Any):
        self.db = db
        with db.connection() as conn:
            conn.executescript(SCHEMA)
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(email_templates)").fetchall()}
            if "html" not in cols:                       # v13.52: o formato (HTML) do modelo
                conn.execute("ALTER TABLE email_templates ADD COLUMN html TEXT NOT NULL DEFAULT ''")

    # ---------------- cadastro
    def list(self) -> List[Dict[str, Any]]:
        with self.db.connection() as conn:
            rows = conn.execute("SELECT * FROM email_templates ORDER BY usos DESC, nome").fetchall()
        out = []
        for r in rows:
            item = dict(r, campos=fields_of(r["assunto"] + "\n" + r["corpo"] + "\n" + (r["html"] or "")),
                        tem_html=bool(r["html"]))
            item.pop("html", None)
            out.append(item)
        return out

    def get(self, n: Any) -> Dict[str, Any]:
        try:
            num = int(str(n).strip().lstrip("#"))
        except ValueError:
            raise ValueError("Use o número do modelo (veja /modelos_email).")
        with self.db.connection() as conn:
            row = conn.execute("SELECT * FROM email_templates WHERE n = ?", (num,)).fetchone()
        if row is None:
            raise ValueError(f"Não há modelo #{num}.")
        return dict(row, campos=fields_of(row["assunto"] + "\n" + row["corpo"] + "\n" + (row["html"] or "")),
                    tem_html=bool(row["html"]))

    def save(self, nome: str, corpo: str, uso: str = "", assunto: str = "", origem: str = "manual",
             html: Optional[str] = None) -> Dict[str, Any]:
        """html=None mantém o formato que o modelo já tinha; html="" remove; texto vazio com HTML vira o texto do HTML."""
        nome = " ".join(str(nome or "").split())[:80]
        if html:
            html = clean_html(html)
            if len(str(corpo or "").strip()) < 10:
                corpo = html_text(html)
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
            conn.execute("INSERT INTO email_templates (nome, uso, assunto, corpo, origem, criado, html) VALUES (?, ?, ?, ?, ?, ?, ?) "
                         "ON CONFLICT(nome) DO UPDATE SET uso = excluded.uso, assunto = excluded.assunto, "
                         "corpo = excluded.corpo, origem = excluded.origem"
                         + ("" if html is None else ", html = excluded.html"),
                         (nome, uso, assunto, corpo, origem, time.time(), html or ""))
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

    def set_signature(self, text: str, html: Optional[str] = None) -> str:
        text = str(text or "").replace("\r\n", "\n").strip()[:1500]
        if html:
            html = clean_html(html)
            if len(html) > 400_000:
                raise ValueError("A assinatura em HTML ficou grande demais (imagem pesada).")
            text = text or html_text(html)[:1500]
        self.db.set_setting(SETTING_SIGNATURE, text)
        if html is not None:
            self.db.set_setting(SETTING_SIGNATURE_HTML, html or "")
        return text

    def signature_html(self) -> str:
        return str(self.db.get_setting(SETTING_SIGNATURE_HTML, "") or "")

    # ---------------- o formato (HTML) do modelo no e-mail pronto
    def render_html(self, tpl: Dict[str, Any], subject: str, body: str, fields: Dict[str, Any],
                    llm: Optional[Callable[[str, str], str]] = None, rewrite: bool = False) -> Tuple[str, str]:
        """(html pronto, como foi feito). Com {{campos}}: troca direto no HTML (o formato fica idêntico). Sem campos e com
        HTML não muito grande: a LLM troca SÓ os textos e a estrutura é conferida. Senão: '' (fica só o texto)."""
        html = str(tpl.get("html") or "")
        if not html:
            return "", ""
        if fields_of(html):
            out = fill_fields(html, fields, html=True)
            if not rewrite or llm is None:
                return self.add_signature_html(out), "campos"
            html = out                    # v13.54: ajuste pedido ("mais curto", "troca o parágrafo"): os textos também mudam
        light, images = hide_images(html)
        if llm is None or len(light) > LLM_HTML_LIMIT:
            return "", "grande"
        raw = llm("Você edita e-mails em HTML. Devolva SOMENTE o HTML do modelo com os TEXTOS trocados para dizer o que o "
                  "e-mail novo diz. Mantenha TODAS as tags, atributos, estilos, tabelas, cores, imagens e links exatamente "
                  "como estão; mude só o texto entre as tags (e acrescente parágrafos copiando o estilo de um existente, se "
                  "precisar). Nada de markdown nem explicações.",
                  f"<modelo_html>\n{light}\n</modelo_html>\n\nE-MAIL NOVO (texto):\nAssunto: {subject}\n\n{body}")
        out = re.sub(r"^```(?:html)?\s*|\s*```$", "", str(raw or "").strip())
        if not out.startswith("<") or tag_count(out) < 0.8 * tag_count(light):
            return "", "estrutura"
        return self.add_signature_html(clean_html(show_images(out, images))), "texto"

    def add_signature_html(self, html: str) -> str:
        sig_text = self.signature()
        if not sig_text or sig_text.split("\n")[0] in html_text(html):
            return html
        sig_html = self.signature_html() or "<p>" + "<br>".join(__import__("html").escape(x) for x in sig_text.split("\n")) + "</p>"
        if re.search(r"(?i)</body>", html):
            return re.sub(r"(?i)</body>", sig_html + "</body>", html, count=1)
        return html + sig_html

    # ---------------- escolher e orientar
    def choose(self, situation: str, llm: Optional[Callable[[str, str], str]] = None,
               request: str = "") -> Optional[Dict[str, Any]]:
        """O modelo que combina com a situação. Sem nenhum parecido, None (aí vale só o estilo).
        v13.54: `request` é o que o USUÁRIO escreveu: se ele cita o nome de um modelo, é esse; se diz "no meu modelo",
        "o modelo que eu uso"..., sempre sai um modelo (o único, o que combina ou o mais usado)."""
        items = self.list()
        if not items:
            return None
        said = fold(request or situation)
        named = [t for t in items if len(fold(t["nome"])) >= 4 and fold(t["nome"]) in said]
        if named:
            return max(named, key=lambda t: len(t["nome"]))
        asks_model = bool(MODEL_REF.search(said)) and not NO_MODEL.search(said)
        if asks_model and len(items) == 1:
            return items[0]
        want = words(situation)
        scored: List[Tuple[float, Dict[str, Any]]] = []
        for t in items:
            key = words(f"{t['nome']} {t['uso']}")
            subject = words(t["assunto"])
            score = 2.0 * len(want & key) + 0.5 * len(want & subject)
            if score:
                scored.append((score, t))
        scored.sort(key=lambda x: -x[0])
        if llm is not None and len(items) > 1 and (asks_model or not scored or len(scored) > 1):
            menu = "\n".join(f"{t['n']}: {t['nome']} — {t['uso'] or 'sem descrição'} (assunto: {t['assunto'] or '-'})"
                             for t in items[:40])
            rule = ("O usuário pediu um dos modelos dele: responda SÓ o número do que mais serve (nunca 0)." if asks_model else
                    "Responda SÓ o número do modelo, ou 0 se nenhum servir de verdade.")
            try:
                raw = llm("Escolha o modelo de e-mail do usuário que serve para a situação. " + rule,
                          f"PEDIDO DO USUÁRIO: {(request or situation)[:600]}\nSITUAÇÃO: {situation[:1500]}\n\nMODELOS:\n{menu}")
                num = int(re.findall(r"\d+", str(raw or "0"))[0]) if re.findall(r"\d+", str(raw or "")) else -1
                if num == 0 and not asks_model:
                    return None
                match = next((t for t in items if t["n"] == num), None)
                if match is not None:
                    return match
            except Exception as err:
                from .falhas import note
                note("Escolha do modelo de e-mail (IA)", err)
        if asks_model:
            return scored[0][1] if scored else items[0]           # list() já vem do mais usado para o menos
        return scored[0][1] if scored and scored[0][0] >= 2.0 else None

    def style(self) -> str:
        """Saudações e despedidas que o usuário usa (tiradas dos próprios modelos, sem custo de LLM)."""
        greet, close = [], []
        for t in self.list()[:30]:
            lines = [ln.strip() for ln in t["corpo"].split("\n") if ln.strip()]
            if lines:
                if len(lines[0]) <= 60 and lines[0] not in greet:
                    greet.append(lines[0])
                tail = [ln for ln in lines[-4:] if len(ln) <= 40 and not ln.startswith(("{{", "["))]
                if tail and tail[0] not in close:
                    close.append(tail[0])
        out = []
        if greet:
            out.append("saudações que ele costuma usar: " + " | ".join(greet[:5]))
        if close:
            out.append("despedidas: " + " | ".join(close[:5]))
        return "; ".join(out)

    def guidance(self, situation: str, llm: Optional[Callable[[str, str], str]] = None, request: str = "",
                 tpl_n: Optional[int] = None) -> Tuple[Optional[Dict[str, Any]], str]:
        """(modelo escolhido, bloco para o prompt de quem escreve o e-mail). tpl_n: um modelo já decidido (ajustes)."""
        tpl = None
        if tpl_n is not None:
            try:
                tpl = self.get(tpl_n)
            except ValueError:
                tpl = None
        if tpl is None:
            tpl = self.choose(situation, llm, request)
        sig = self.signature()
        parts: List[str] = []
        if tpl is not None:
            parts.append(
                f"MODELO DO USUÁRIO A SEGUIR (\"{tpl['nome']}\"): use-o como base. Mantenha o texto, a estrutura, as expressões "
                "e o tom dele; preencha os campos ({{nome}} ou [Nome]) e os trechos que variam com os dados da situação; mude só o que a "
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
        recent = []
        try:                                                     # v13.54: "esse cliente", "aquele modelo"... vêm da conversa
            recent = [str(m.get("content") or "")[:400] for m in service.context_manager.get_window_list()[-6:]
                      if m.get("role") == "user"][-3:]
        except Exception:
            pass
        tpl, block = templates.guidance("\n".join(recent + [message]), request=message)
        if tpl is not None:
            turn.setdefault("email_template", tpl["n"])
            if tpl.get("tem_html"):
                block += ("\n(Este modelo tem o formato do Outlook — tabelas, cores, imagens. Para o e-mail sair formatado, "
                          "o usuário pode pedir pelo Bit no PC, que abre pronto no Outlook.)")
        return block or None

    service.pre_turn_hooks.append(hook)
    service.extra_actions.update({
        "email_templates_list": lambda p: {"modelos": templates.list(), "assinatura": templates.signature(),
                                           "assinatura_html": bool(templates.signature_html()),
                                           "text": format_list(templates.list(), templates.signature())},
        "email_template_save": lambda p: {"modelo": templates.save(str(p.get("nome") or ""), str(p.get("corpo") or ""),
                                                                   str(p.get("uso") or ""), str(p.get("assunto") or ""),
                                                                   str(p.get("origem") or "manual"),
                                                                   p.get("html") if "html" in p else None),
                                          "modelos": templates.list()},
        "email_template_delete": lambda p: {"apagado": templates.delete(p.get("n")), "modelos": templates.list()},
        "email_signature": lambda p: {"assinatura": templates.set_signature(
            str(p.get("assinatura") or ""), p.get("assinatura_html") if "assinatura_html" in p else None)
            if "assinatura" in p else templates.signature()},
    })
    return templates

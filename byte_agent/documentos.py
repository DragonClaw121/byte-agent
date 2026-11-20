"""Planilhas e documentos de verdade — Fase 2.1 (v13.26), no perfil de trabalho (Bit).

  create_spreadsheet  planilha .xlsx com colunas tipadas (texto, número, moeda, %, data), FÓRMULAS (escritas em
                      português ou inglês: SOMA/SUM, SE/IF...), linha de total, filtros, painel congelado e gráfico
                      (barras, linhas ou pizza). Abre no Excel, no Google Planilhas e no LibreOffice.
  create_document     documento .docx com título, seções, parágrafos, listas e tabelas.
  modelos             um .docx ou .xlsx SEU (enviado para arquivos/) com marcadores {{campo}}: o Bit preenche os campos
                      e mantém o seu visual (logo, cabeçalho, rodapé, fontes). {{conteudo}} num .docx marca onde as
                      seções entram. Campo que ficou sem valor vira [PREENCHER: campo] e é avisado.

Segurança: as fórmulas passam por uma lista de funções permitidas (cálculo apenas): nada de HIPERLINK, WEBSERVICE,
links para outros arquivos ou DDE. Arquivos só são lidos de arquivos/ e gravados em entregas/.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

PLACEHOLDER_RE = re.compile(r"\{\{\s*([\wÀ-ÿ][\wÀ-ÿ .\-]{0,40}?)\s*\}\}")
COLUMN_TYPES = ("texto", "numero", "moeda", "porcentagem", "data")
NUMBER_FORMATS = {"numero": "#,##0.##", "moeda": '"R$" #,##0.00', "porcentagem": "0.0%", "data": "DD/MM/YYYY"}
MAX_SHEETS, MAX_COLS, MAX_ROWS = 5, 30, 2000
MAX_SECTIONS, MAX_PARAGRAPHS = 40, 400

# funções de CÁLCULO permitidas nas fórmulas (nomes do Excel em inglês, como o arquivo guarda)
ALLOWED_FUNCTIONS = {
    "SUM", "AVERAGE", "MIN", "MAX", "COUNT", "COUNTA", "COUNTIF", "COUNTIFS", "SUMIF", "SUMIFS", "AVERAGEIF",
    "AVERAGEIFS", "IF", "IFS", "IFERROR", "AND", "OR", "NOT", "ROUND", "ROUNDUP", "ROUNDDOWN", "ABS", "INT", "MOD",
    "POWER", "SQRT", "TODAY", "NOW", "DATE", "YEAR", "MONTH", "DAY", "EDATE", "EOMONTH", "DATEDIF", "NETWORKDAYS",
    "WEEKDAY", "TEXT", "CONCATENATE", "CONCAT", "LEFT", "RIGHT", "MID", "LEN", "UPPER", "LOWER", "TRIM", "VLOOKUP",
    "HLOOKUP", "XLOOKUP", "INDEX", "MATCH", "PMT", "FV", "PV", "RATE", "NPV", "IRR", "SUMPRODUCT", "MEDIAN", "LARGE",
    "SMALL", "RANK", "PERCENTILE",
}
PT_FUNCTIONS = {
    "SOMA": "SUM", "MEDIA": "AVERAGE", "MÉDIA": "AVERAGE", "MINIMO": "MIN", "MÍNIMO": "MIN", "MAXIMO": "MAX",
    "MÁXIMO": "MAX", "CONT.NUM": "COUNT", "CONT.NÚM": "COUNT", "CONT.VALORES": "COUNTA", "CONT.SE": "COUNTIF",
    "CONT.SES": "COUNTIFS", "SOMASE": "SUMIF", "SOMASES": "SUMIFS", "MEDIASE": "AVERAGEIF", "MÉDIASE": "AVERAGEIF",
    "MEDIASES": "AVERAGEIFS", "MÉDIASES": "AVERAGEIFS", "SE": "IF", "SES": "IFS", "SEERRO": "IFERROR", "E": "AND",
    "OU": "OR", "NAO": "NOT", "NÃO": "NOT", "ARRED": "ROUND", "ARREDONDAR.PARA.CIMA": "ROUNDUP",
    "ARREDONDAR.PARA.BAIXO": "ROUNDDOWN", "INT": "INT", "RESTO": "MOD", "POTENCIA": "POWER", "POTÊNCIA": "POWER",
    "RAIZ": "SQRT", "HOJE": "TODAY", "AGORA": "NOW", "DATA": "DATE", "ANO": "YEAR", "MES": "MONTH", "MÊS": "MONTH",
    "DIA": "DAY", "DATAM": "EDATE", "FIMMES": "EOMONTH", "FIMMÊS": "EOMONTH", "DATADIF": "DATEDIF",
    "DIATRABALHOTOTAL": "NETWORKDAYS", "DIA.DA.SEMANA": "WEEKDAY", "TEXTO": "TEXT", "CONCATENAR": "CONCATENATE",
    "ESQUERDA": "LEFT", "DIREITA": "RIGHT", "EXT.TEXTO": "MID", "NUM.CARACT": "LEN", "MAIUSCULA": "UPPER",
    "MAIÚSCULA": "UPPER", "MINUSCULA": "LOWER", "MINÚSCULA": "LOWER", "ARRUMAR": "TRIM", "PROCV": "VLOOKUP",
    "PROCH": "HLOOKUP", "PROCX": "XLOOKUP", "INDICE": "INDEX", "ÍNDICE": "INDEX", "CORRESP": "MATCH", "PGTO": "PMT",
    "VF": "FV", "VP": "PV", "TAXA": "RATE", "VPL": "NPV", "TIR": "IRR", "SOMARPRODUTO": "SUMPRODUCT", "MED": "MEDIAN",
    "MAIOR": "LARGE", "MENOR": "SMALL", "ORDEM": "RANK", "PERCENTIL": "PERCENTILE",
}
_FUNC_RE = re.compile(r"([A-Za-zÀ-ÿ][A-Za-zÀ-ÿ0-9_.]*)\s*\(")


class FormulaError(ValueError):
    pass


def _clean(text: Any, limit: int) -> str:
    return " ".join(str(text if text is not None else "").split())[:limit]


def safe_formula(raw: str, row: Optional[int] = None) -> str:
    """Normaliza (português -> inglês, ';' -> ',', {linha} -> número) e recusa o que não for cálculo."""
    f = str(raw or "").strip()
    if not f.startswith("="):
        f = "=" + f
    if len(f) > 500:
        raise FormulaError("Fórmula longa demais.")
    if row is not None:
        f = re.sub(r"\{\s*(linha|row|l)\s*\}", str(row), f, flags=re.I)
    if re.search(r"[\[\]|\\{}]|https?:|://|\bfile:", f, re.I):
        raise FormulaError("Fórmula com link externo ou caractere não permitido.")
    # fora das aspas: ';' vira ',' (separador do Excel em inglês) e nomes de função em português viram inglês
    parts = re.split(r'("[^"]*")', f)
    out = []
    for i, part in enumerate(parts):
        if i % 2:                                    # texto entre aspas: fica como está
            out.append(part)
            continue
        part = part.replace(";", ",")

        def fix(m: "re.Match[str]") -> str:
            name = m.group(1).upper()
            name = PT_FUNCTIONS.get(name, name)
            if name not in ALLOWED_FUNCTIONS:
                raise FormulaError(f"Função não permitida na planilha: {m.group(1)}.")
            return name + "("
        out.append(_FUNC_RE.sub(fix, part))
    return "".join(out)


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value or "").strip().replace("R$", "").replace("%", "").replace(" ", "")
    if not text:
        return None
    if re.fullmatch(r"-?\d{1,3}(\.\d{3})+(,\d+)?|-?\d+,\d+", text):      # 1.234,56 ou 12,5 (brasileiro)
        text = text.replace(".", "").replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return None


def _date(value: Any) -> Optional[date]:
    if isinstance(value, (date, datetime)):
        return value if isinstance(value, date) else value.date()
    text = str(value or "").strip()
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d/%m/%y", "%d-%m-%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _cell_value(kind: str, value: Any) -> Any:
    if isinstance(value, str) and value.strip().startswith("="):
        return value                                  # fórmula: tratada por quem chamou
    if kind in {"numero", "moeda"}:
        n = _number(value)
        return value if n is None else n
    if kind == "porcentagem":
        n = _number(value)
        if n is None:
            return value
        return n / 100.0 if (isinstance(value, str) and "%" in value) or abs(n) > 1 else n
    if kind == "data":
        return _date(value) or value
    return "" if value is None else str(value)[:1000]


# ---------------------------------------------------------------- planilha
def _columns(raw: Any) -> List[Dict[str, Any]]:
    cols = []
    for c in list(raw or [])[:MAX_COLS]:
        if isinstance(c, str):
            c = {"nome": c}
        if not isinstance(c, dict):
            continue
        kind = str(c.get("tipo") or "texto").lower().replace("número", "numero").replace("percentual", "porcentagem")
        cols.append({"nome": _clean(c.get("nome") or c.get("name") or f"Coluna {len(cols) + 1}", 60),
                     "tipo": kind if kind in COLUMN_TYPES else "texto",
                     "formula": str(c.get("formula") or "").strip()})
    if not cols:
        raise ValueError("Informe as colunas da planilha.")
    return cols


def write_spreadsheet(path: Path, spec: Dict[str, Any]) -> Dict[str, Any]:
    import openpyxl  # type: ignore
    from openpyxl.chart import BarChart, LineChart, PieChart, Reference  # type: ignore
    from openpyxl.styles import Alignment, Font, PatternFill  # type: ignore
    from openpyxl.utils import get_column_letter  # type: ignore

    sheets = spec.get("abas") or [spec]
    book = openpyxl.Workbook()
    book.remove(book.active)
    head_fill = PatternFill("solid", fgColor="1F3A5F")
    report = {"abas": [], "formulas": 0, "graficos": 0}
    for s_idx, sheet_spec in enumerate(list(sheets)[:MAX_SHEETS]):
        name = re.sub(r"[\[\]:*?/\\]", "-", _clean(sheet_spec.get("nome") or spec.get("titulo") or f"Aba {s_idx + 1}", 31)) or f"Aba {s_idx + 1}"
        ws = book.create_sheet(name)
        cols = _columns(sheet_spec.get("colunas"))
        rows = [r for r in list(sheet_spec.get("linhas") or [])[:MAX_ROWS] if isinstance(r, (list, dict))]
        start = 1
        title = _clean(sheet_spec.get("titulo") or (spec.get("titulo") if s_idx == 0 else ""), 120)
        if title:
            ws.cell(row=1, column=1, value=title).font = Font(bold=True, size=14)
            start = 3
        for j, col in enumerate(cols, start=1):
            cell = ws.cell(row=start, column=j, value=col["nome"])
            cell.font, cell.fill = Font(bold=True, color="FFFFFF"), head_fill
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        first = start + 1
        for i, row in enumerate(rows):
            r = first + i
            values = [row.get(c["nome"]) for c in cols] if isinstance(row, dict) else list(row) + [None] * len(cols)
            for j, col in enumerate(cols, start=1):
                value = values[j - 1]
                if col["formula"] and (value is None or value == ""):
                    value = col["formula"]
                if isinstance(value, str) and value.strip().startswith("="):
                    value = safe_formula(value, r)
                    report["formulas"] += 1
                else:
                    value = _cell_value(col["tipo"], value)
                cell = ws.cell(row=r, column=j, value=value)
                if col["tipo"] in NUMBER_FORMATS:
                    cell.number_format = NUMBER_FORMATS[col["tipo"]]
        last = first + len(rows) - 1
        total_row = None
        if sheet_spec.get("total", spec.get("total", True)) and rows:
            total_row = last + 1
            ws.cell(row=total_row, column=1, value="Total").font = Font(bold=True)
            for j, col in enumerate(cols, start=1):
                if j == 1 or col["tipo"] not in {"numero", "moeda"}:
                    continue
                letter = get_column_letter(j)
                cell = ws.cell(row=total_row, column=j, value=f"=SUM({letter}{first}:{letter}{last})")
                cell.font, cell.number_format = Font(bold=True), NUMBER_FORMATS[col["tipo"]]
                report["formulas"] += 1
        for k, extra in enumerate(list(sheet_spec.get("resumo") or [])[:20]):     # linhas livres abaixo: [rótulo, fórmula]
            if not isinstance(extra, (list, tuple)) or len(extra) < 2:
                continue
            r = ws.max_row + (2 if k == 0 else 1)
            ws.cell(row=r, column=1, value=_clean(extra[0], 80)).font = Font(bold=True)
            value = extra[1]
            if isinstance(value, str) and value.strip().startswith("="):
                value = safe_formula(value)
                report["formulas"] += 1
            cell = ws.cell(row=r, column=2, value=value)
            if isinstance(value, str) and value.startswith("=") and not re.match(r"=(COUNT|TODAY|NOW|TEXT|IF)", value):
                cell.number_format = "#,##0.00"
        for j, col in enumerate(cols, start=1):
            longest = max([len(col["nome"])] + [len(str(ws.cell(row=r, column=j).value or ""))
                                                for r in range(first, min(last, first + 200) + 1)])
            floor = {"moeda": 16, "numero": 12, "porcentagem": 10, "data": 12}.get(col["tipo"], 10)  # cabe o total formatado
            ws.column_dimensions[get_column_letter(j)].width = max(floor, min(50, longest + 2))
        if rows:
            ws.freeze_panes = ws.cell(row=first, column=1)
            ws.auto_filter.ref = f"A{start}:{get_column_letter(len(cols))}{last}"
        chart_spec = sheet_spec.get("grafico") or {}
        if chart_spec and rows:
            names = [c["nome"].lower() for c in cols]
            cat_name = str(chart_spec.get("categorias") or cols[0]["nome"]).lower()
            val_names = [str(v).lower() for v in (chart_spec.get("valores") or [])] or \
                        [c["nome"].lower() for c in cols[1:] if c["tipo"] in {"numero", "moeda", "porcentagem"}][:3]
            if cat_name in names and any(v in names for v in val_names):
                kind = str(chart_spec.get("tipo") or "barras").lower()
                chart = PieChart() if kind.startswith("pizza") else LineChart() if kind.startswith("linha") else BarChart()
                chart.title = _clean(chart_spec.get("titulo") or title or name, 80)
                chart.height, chart.width = 8, 16
                cat_col = names.index(cat_name) + 1
                for v in val_names[: (1 if isinstance(chart, PieChart) else 4)]:
                    if v in names:
                        j = names.index(v) + 1
                        chart.add_data(Reference(ws, min_col=j, min_row=start, max_row=last), titles_from_data=True)
                chart.set_categories(Reference(ws, min_col=cat_col, min_row=first, max_row=last))
                ws.add_chart(chart, f"{get_column_letter(len(cols) + 2)}{start}")
                report["graficos"] += 1
        report["abas"].append({"nome": name, "linhas": len(rows), "colunas": len(cols), "total": total_row is not None})
    if not report["abas"]:
        raise ValueError("A planilha precisa de pelo menos uma aba com colunas.")
    path.parent.mkdir(parents=True, exist_ok=True)
    book.save(str(path))
    return report


# ---------------------------------------------------------------- documento
def _docx():
    try:
        import docx  # type: ignore
        return docx
    except ImportError as err:
        raise RuntimeError("Para criar documentos Word instale o pacote python-docx (pip install python-docx).") from err


def _add_table(doc: Any, table_spec: Dict[str, Any]) -> None:
    cols = [_clean(c.get("nome") if isinstance(c, dict) else c, 60) for c in list(table_spec.get("colunas") or [])[:12]]
    rows = [r for r in list(table_spec.get("linhas") or [])[:300] if isinstance(r, (list, tuple))]
    if not cols:
        return
    table = doc.add_table(rows=1, cols=len(cols))
    try:
        table.style = "Table Grid"
    except Exception:
        pass
    for j, name in enumerate(cols):
        cell = table.rows[0].cells[j]
        cell.text = name
        for run in cell.paragraphs[0].runs:
            run.bold = True
    for row in rows:
        cells = table.add_row().cells
        for j in range(len(cols)):
            cells[j].text = _clean(row[j] if j < len(row) else "", 500)


def _styled(doc: Any, text: str, style: str, prefix: str = "") -> Any:
    """Parágrafo com o estilo pedido; se o modelo do usuário não tiver esse estilo, parágrafo simples."""
    try:
        return doc.add_paragraph(text, style=style)
    except KeyError:
        return doc.add_paragraph(prefix + text)


def _heading(doc: Any, text: str, level: int) -> Any:
    try:
        return doc.add_heading(text, level=level)
    except KeyError:
        par = doc.add_paragraph()
        run = par.add_run(text)
        run.bold = True
        return par


def _add_sections(doc: Any, sections: List[Dict[str, Any]], level_offset: int = 0) -> int:
    count = 0
    for sec in list(sections or [])[:MAX_SECTIONS]:
        if not isinstance(sec, dict):
            continue
        heading = _clean(sec.get("titulo") or sec.get("title"), 160)
        if heading:
            _heading(doc, heading, min(3, 1 + level_offset + int(sec.get("nivel", 0) or 0)))
        for para in list(sec.get("paragrafos") or [])[:MAX_PARAGRAPHS]:
            text = str(para or "").strip()
            if text:
                doc.add_paragraph(text[:5000])
                count += 1
        for item in list(sec.get("itens") or [])[:100]:
            text = str(item or "").strip()
            if text:
                _styled(doc, text[:1000], "List Bullet", "• ")
                count += 1
        for item in list(sec.get("numerados") or [])[:100]:
            text = str(item or "").strip()
            if text:
                _styled(doc, text[:1000], "List Number", f"{count + 1}. ")
                count += 1
        if isinstance(sec.get("tabela"), dict):
            _add_table(doc, sec["tabela"])
            doc.add_paragraph()                       # respiro depois da tabela
            count += 1
    return count


def _paragraphs_everywhere(doc: Any) -> List[Any]:
    out = list(doc.paragraphs)
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                out.extend(cell.paragraphs)
    for section in doc.sections:
        for part in (section.header, section.footer):
            out.extend(part.paragraphs)
            for table in part.tables:
                for row in table.rows:
                    for cell in row.cells:
                        out.extend(cell.paragraphs)
    return out


def _fill_paragraph(par: Any, fields: Dict[str, str], missing: set) -> None:
    text = "".join(r.text for r in par.runs)
    if "{{" not in text:
        return

    def repl(m: "re.Match[str]") -> str:
        key = m.group(1).strip()
        value = fields.get(key.lower())
        if value is None:
            missing.add(key)
            return f"[PREENCHER: {key}]"
        return value
    # 1º: troca dentro de cada trecho (mantém negrito, cor, fonte de cada um)
    for run in par.runs:
        if "{{" in run.text:
            run.text = PLACEHOLDER_RE.sub(repl, run.text)
    rest = "".join(r.text for r in par.runs)
    if not PLACEHOLDER_RE.search(rest):
        return
    # 2º: o Word às vezes quebra um {{campo}} em vários trechos: junta no primeiro
    par.runs[0].text = PLACEHOLDER_RE.sub(repl, rest)
    for run in par.runs[1:]:
        run.text = ""


def template_fields(path: Path) -> List[str]:
    """Os {{campos}} de um modelo .docx ou .xlsx (na ordem em que aparecem)."""
    found: List[str] = []
    if path.suffix.lower() == ".docx":
        doc = _docx().Document(str(path))
        texts = ["".join(r.text for r in p.runs) for p in _paragraphs_everywhere(doc)]
    elif path.suffix.lower() == ".xlsx":
        import openpyxl  # type: ignore
        book = openpyxl.load_workbook(str(path))
        texts = [str(c.value) for ws in book.worksheets for row in ws.iter_rows() for c in row if isinstance(c.value, str)]
    else:
        return []
    for t in texts:
        for m in PLACEHOLDER_RE.finditer(t):
            key = m.group(1).strip()
            if key.lower() not in {f.lower() for f in found}:
                found.append(key)
    return found


def write_document(path: Path, spec: Dict[str, Any], template: Optional[Path] = None) -> Dict[str, Any]:
    docx = _docx()
    fields = {str(k).strip().lower(): _clean(v, 2000) for k, v in dict(spec.get("campos") or {}).items()}
    missing: set = set()
    sections = list(spec.get("secoes") or [])
    title = _clean(spec.get("titulo"), 160)
    if template is not None:
        doc = docx.Document(str(template))
        marker = next((p for p in doc.paragraphs if re.search(r"\{\{\s*conte[uú]do\s*\}\}", p.text, re.I)), None)
        for par in _paragraphs_everywhere(doc):
            if marker is None or par._p is not marker._p:
                _fill_paragraph(par, fields, missing)
        if sections or marker is not None:
            before = list(doc.element.body)          # referências fortes: o lxml devolve o mesmo objeto para cada nó
            added = _add_sections(doc, sections)
            if marker is not None:
                # as seções foram criadas no fim do corpo: move-as para o lugar do {{conteudo}}
                new_elems = [el for el in doc.element.body if not any(el is b for b in before) and el.tag.endswith(("}p", "}tbl"))]
                for el in new_elems:
                    marker._p.addprevious(el)
                marker._p.getparent().remove(marker._p)
        else:
            added = 0
    else:
        doc = docx.Document()
        if title:
            _heading(doc, title, 0)
        if spec.get("subtitulo"):
            doc.add_paragraph().add_run(_clean(spec["subtitulo"], 300)).italic = True
        added = _add_sections(doc, sections)
    if title:
        doc.core_properties.title = title
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(path))
    return {"blocos": added, "faltando": sorted(missing), "modelo": template.name if template else None}


def fill_xlsx_template(path: Path, template: Path, fields: Dict[str, Any]) -> Dict[str, Any]:
    import openpyxl  # type: ignore
    values = {str(k).strip().lower(): v for k, v in dict(fields or {}).items()}
    missing: set = set()
    book = openpyxl.load_workbook(str(template))
    filled = 0
    for ws in book.worksheets:
        for row in ws.iter_rows():
            for cell in row:
                if not isinstance(cell.value, str) or "{{" not in cell.value:
                    continue
                whole = PLACEHOLDER_RE.fullmatch(cell.value.strip())
                if whole and whole.group(1).strip().lower() in values:
                    raw = values[whole.group(1).strip().lower()]
                    n = _number(raw) if not isinstance(raw, str) or not re.search(r"[A-Za-z]", raw) else None
                    cell.value = n if n is not None else (_date(raw) or _clean(raw, 2000))
                    filled += 1
                    continue

                def repl(m: "re.Match[str]") -> str:
                    key = m.group(1).strip()
                    if key.lower() not in values:
                        missing.add(key)
                        return f"[PREENCHER: {key}]"
                    return _clean(values[key.lower()], 2000)
                cell.value = PLACEHOLDER_RE.sub(repl, cell.value)
                filled += 1
    path.parent.mkdir(parents=True, exist_ok=True)
    book.save(str(path))
    return {"preenchidos": filled, "faltando": sorted(missing), "modelo": template.name}


# ---------------------------------------------------------------- ferramentas do Bit
def install(work: Any) -> None:
    import json

    from .work import slugify
    service = work.service

    def stem(kind: str, title: str) -> str:
        return f"{kind}_{slugify(title or kind)}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

    def templates() -> List[Dict[str, Any]]:
        out = []
        for item in work._listing(work.inputs, {".docx", ".xlsx"}):
            try:
                fields = template_fields(work.inputs / item["name"])
            except Exception:
                continue
            if fields or item["name"].lower().startswith("modelo"):
                out.append({"name": item["name"], "campos": fields})
        return out

    def spreadsheet(args: Dict[str, Any]) -> Dict[str, Any]:
        title = _clean(args.get("titulo") or args.get("title") or "Planilha", 120)
        if args.get("modelo"):
            tpl = work.resolve_input(str(args["modelo"]))
            if tpl.suffix.lower() != ".xlsx":
                raise ValueError("O modelo de planilha precisa ser .xlsx.")
            name = stem("planilha", title) + ".xlsx"
            rep = fill_xlsx_template(work.outputs / name, tpl, args.get("campos") or {})
        else:
            name = stem("planilha", title) + ".xlsx"
            rep = write_spreadsheet(work.outputs / name, dict(args, titulo=title))
        service.db.record_audit("work_spreadsheet", "create_spreadsheet", "success", {"title": title, "file": name})
        return dict(rep, arquivo=name, titulo=title)

    def document(args: Dict[str, Any]) -> Dict[str, Any]:
        title = _clean(args.get("titulo") or args.get("title") or "Documento", 160)
        tpl = None
        if args.get("modelo"):
            tpl = work.resolve_input(str(args["modelo"]))
            if tpl.suffix.lower() != ".docx":
                raise ValueError("O modelo de documento precisa ser .docx.")
        elif not args.get("secoes"):
            raise ValueError("Informe as seções do documento (ou um modelo .docx com {{campos}}).")
        name = stem("documento", title) + ".docx"
        rep = write_document(work.outputs / name, dict(args, titulo=title), tpl)
        service.db.record_audit("work_document_created", "create_document", "success", {"title": title, "file": name})
        return dict(rep, arquivo=name, titulo=title)

    def reply(res: Dict[str, Any], what: str) -> str:
        warn = ""
        if res.get("faltando"):
            warn = ("\n⚠️ Campos sem valor (ficaram como [PREENCHER: ...]): " + ", ".join(res["faltando"]))
        return f"{what} '{res['titulo']}' pronta.{warn}\nArquivos gerados em entregas/: {res['arquivo']}"

    def tool_sheet(args: Dict[str, Any], turn: Dict[str, Any]) -> str:
        try:
            res = spreadsheet(args)
        except (ValueError, FileNotFoundError, RuntimeError) as err:
            return f"ERRO: {err}"
        turn.setdefault("blocks", []).append(reply(res, "Planilha"))
        return json.dumps({"arquivo": res["arquivo"], "abas": res.get("abas"), "formulas": res.get("formulas"),
                           "faltando": res.get("faltando", []),
                           "instrucao": "O arquivo já foi entregue ao usuário. Diga em 1-2 frases o que tem na planilha."},
                          ensure_ascii=False)

    def tool_doc(args: Dict[str, Any], turn: Dict[str, Any]) -> str:
        try:
            res = document(args)
        except (ValueError, FileNotFoundError, RuntimeError) as err:
            return f"ERRO: {err}"
        turn.setdefault("blocks", []).append(reply(res, "Documento").replace("pronta", "pronto"))
        return json.dumps({"arquivo": res["arquivo"], "faltando": res["faltando"],
                           "instrucao": ("O arquivo já foi entregue ao usuário. Diga em 1-2 frases o que tem nele"
                                         + (" e peça os campos que faltaram." if res["faltando"] else "."))}, ensure_ascii=False)

    def tool_templates(args: Dict[str, Any], turn: Dict[str, Any]) -> str:
        return json.dumps({"modelos": templates(), "dica": "Use o nome em 'modelo' e preencha 'campos' com cada um."},
                          ensure_ascii=False)

    column_schema = {"type": "object", "properties": {
        "nome": {"type": "string"},
        "tipo": {"type": "string", "enum": list(COLUMN_TYPES)},
        "formula": {"type": "string", "description": "Fórmula para TODAS as linhas, com {linha}: ex. =B{linha}*C{linha}"}}}
    section_schema = {"type": "object", "properties": {
        "titulo": {"type": "string"}, "paragrafos": {"type": "array", "items": {"type": "string"}},
        "itens": {"type": "array", "items": {"type": "string"}, "description": "lista com marcadores"},
        "numerados": {"type": "array", "items": {"type": "string"}},
        "tabela": {"type": "object", "properties": {"colunas": {"type": "array", "items": {"type": "string"}},
                                                     "linhas": {"type": "array", "items": {"type": "array", "items": {}}}}}}}
    service.extra_tools.update({
        "create_spreadsheet": {
            "auto": True, "mission": True, "handler": tool_sheet,
            "intent": re.compile(r"\b(planilhas?|xlsx|excel|orcamentos?|controle de|tabela de|cronogramas?|fluxo de caixa)\b"),
            "description": ("CRIA uma planilha Excel nova (.xlsx) com colunas tipadas, fórmulas, linha de total e gráfico. Use para "
                            "orçamentos, controles, cronogramas, comparativos. Para ANALISAR uma planilha existente use analyze_data. "
                            "Com 'modelo' (um .xlsx do usuário com {{campos}}), só preenche os campos."),
            "parameters": {"type": "object", "required": ["titulo"], "properties": {
                "titulo": {"type": "string"},
                "colunas": {"type": "array", "items": column_schema},
                "linhas": {"type": "array", "items": {"type": "array", "items": {}},
                           "description": "Valores na ordem das colunas; uma célula pode ser fórmula (=B2*C2). Não invente dados: "
                                          "deixe vazio o que o usuário não informou."},
                "total": {"type": "boolean", "description": "Linha de total com SOMA nas colunas numéricas (padrão true)"},
                "grafico": {"type": "object", "properties": {
                    "tipo": {"type": "string", "enum": ["barras", "linhas", "pizza"]},
                    "categorias": {"type": "string", "description": "coluna dos rótulos"},
                    "valores": {"type": "array", "items": {"type": "string"}}}},
                "abas": {"type": "array", "description": "Para várias abas: cada uma com nome, colunas, linhas, total, grafico",
                         "items": {"type": "object"}},
                "modelo": {"type": "string", "description": "Nome de um modelo .xlsx do usuário (veja list_templates)"},
                "campos": {"type": "object", "description": "Valores dos {{campos}} do modelo"}}}},
        "create_document": {
            "auto": True, "mission": True, "handler": tool_doc,
            "intent": re.compile(r"\b(documentos?|docx|word|contratos?|propostas?|oficios?|cartas?|atas?|memorandos?|"
                                 r"relatorios?|modelos?|curriculo|declarac\w+|recibos?)\b"),
            "description": ("CRIA um documento Word (.docx) com título, seções, parágrafos, listas e tabelas: propostas, relatórios, "
                            "atas, cartas, ofícios. Com 'modelo' (um .docx do usuário com {{campos}}), preenche os campos mantendo o "
                            "visual dele; as seções entram no lugar de {{conteudo}}. Não invente dados: use [PREENCHER] onde faltar."),
            "parameters": {"type": "object", "required": ["titulo"], "properties": {
                "titulo": {"type": "string"}, "subtitulo": {"type": "string"},
                "secoes": {"type": "array", "items": section_schema},
                "modelo": {"type": "string", "description": "Nome de um modelo .docx do usuário (veja list_templates)"},
                "campos": {"type": "object", "description": "Valores dos {{campos}} do modelo, ex.: {\"cliente\": \"ACME\"}"}}}},
        "list_templates": {
            "auto": True, "mission": True, "handler": tool_templates,
            "intent": re.compile(r"\b(modelos?|templates?)\b"),
            "description": "Lista os modelos .docx/.xlsx do usuário e os {{campos}} de cada um.",
            "parameters": {"type": "object", "properties": {}}},
    })
    service.extra_actions.update({
        "work_spreadsheet": lambda payload: spreadsheet(payload),
        "work_document": lambda payload: document(payload),
        "work_templates": lambda payload: {"templates": templates()},
    })
    work.templates = templates

"""Análise de dados e geração de documentos do perfil de trabalho — só biblioteca padrão.

Princípio: TODO número é calculado por código (este módulo). A LLM só redige a interpretação a partir
dos fatos já calculados, e o texto dela é conferido contra esses fatos antes de entrar no relatório.

Leitura: CSV (separador e decimal brasileiros detectados) e XLSX (se `openpyxl` estiver instalado).
Saídas: relatório HTML, apresentação HTML (imprime em PDF pelo navegador) e PPTX (se `python-pptx` estiver instalado).
"""
from __future__ import annotations

import csv
import html
import io
import math
import re
import statistics
import unicodedata
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

MAX_ROWS = 100_000
MAX_FILE_BYTES = 50 * 1024 * 1024

# Paleta do relatório (um único matiz para grandeza; texto sempre em tinta de texto, nunca na cor da série)
INK, INK_2, RULE, SURFACE, SERIES = "#0b0b0b", "#52514e", "#d9d8d2", "#fcfcfb", "#2a78d6"

MEASURE_NAME_RE = re.compile(r"(valor|receita|faturamento|venda|total|montante|preco|ticket|amount|revenue|sales|gasto|custo)")
ENTITY_NAME_RE = re.compile(r"(cliente|customer|razao|empresa|conta|comprador|parceiro)")
ID_NAME_RE = re.compile(r"(^|_|\b)(id|codigo|cod|cpf|cnpj|cep|telefone|fone|celular|matricula|pedido|nf|nota)(_|\b|$)")
DATE_FORMATS = ("%d/%m/%Y", "%Y-%m-%d", "%d/%m/%y", "%d-%m-%Y", "%Y/%m/%d", "%d/%m/%Y %H:%M", "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%dT%H:%M:%S", "%d/%m/%Y %H:%M:%S", "%Y-%m-%d %H:%M")


def fold(text: Any) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", str(text or "").lower()) if not unicodedata.combining(c))


def fmt_num(value: Optional[float], decimals: Optional[int] = None) -> str:
    """Número no padrão brasileiro: 1.234,56. Inteiros saem sem casas decimais."""
    if value is None or (isinstance(value, float) and (math.isnan(value) or math.isinf(value))):
        return "—"
    if decimals is None:
        decimals = 0 if float(value).is_integer() else 2
    text = f"{float(value):,.{decimals}f}"
    return text.replace(",", "§").replace(".", ",").replace("§", ".")


def fmt_pct(value: Optional[float]) -> str:
    return "—" if value is None else f"{fmt_num(value * 100, 1)}%"


# ---------------------------------------------------------------- leitura
class Table:
    def __init__(self, headers: List[str], rows: List[List[str]], source: str = ""):
        self.headers = headers
        self.rows = rows
        self.source = source

    def column(self, index: int) -> List[str]:
        return [row[index] if index < len(row) else "" for row in self.rows]


def _clean_headers(raw: Sequence[Any]) -> List[str]:
    headers: List[str] = []
    for idx, cell in enumerate(raw):
        name = " ".join(str(cell if cell is not None else "").split())[:60] or f"coluna_{idx + 1}"
        base, n = name, 2
        while name in headers:
            name, n = f"{base}_{n}", n + 1
        headers.append(name)
    return headers


def read_table(path: Path) -> Table:
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"Arquivo não encontrado: {path.name}")
    if path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError(f"Arquivo grande demais ({path.stat().st_size // (1024 * 1024)} MB; limite de 50 MB).")
    suffix = path.suffix.lower()
    if suffix == ".xlsx":
        try:
            import openpyxl  # type: ignore
        except ImportError as err:
            raise RuntimeError("Para ler .xlsx instale o pacote openpyxl (pip install openpyxl) ou exporte a planilha como CSV.") from err
        book = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
        sheet = book.worksheets[0]
        raw_rows = []
        for row in sheet.iter_rows(values_only=True):
            raw_rows.append(["" if c is None else (c.strftime("%Y-%m-%d") if isinstance(c, (datetime, date)) else str(c)) for c in row])
            if len(raw_rows) > MAX_ROWS + 1:
                break
        book.close()
    elif suffix in {".csv", ".txt", ".tsv"}:
        data = path.read_bytes()
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = data.decode("cp1252", errors="replace")
        sample = [ln for ln in text.splitlines()[:8] if ln.strip()]
        delimiter = max((";", ",", "\t", "|"), key=lambda d: (min((ln.count(d) for ln in sample), default=0), d == ";"))
        raw_rows = []
        for row in csv.reader(io.StringIO(text), delimiter=delimiter):
            raw_rows.append(row)
            if len(raw_rows) > MAX_ROWS + 1:
                break
    else:
        raise ValueError("Formato não suportado. Use .csv ou .xlsx.")
    raw_rows = [r for r in raw_rows if any(str(c).strip() for c in r)]
    if len(raw_rows) < 2:
        raise ValueError("A planilha precisa de uma linha de cabeçalho e ao menos uma linha de dados.")
    headers = _clean_headers(raw_rows[0])
    width = len(headers)
    rows = [[str(c).strip() for c in (r + [""] * width)[:width]] for r in raw_rows[1:MAX_ROWS + 1]]
    return Table(headers, rows, source=path.name)


# ---------------------------------------------------------------- interpretação de células
_NUM_CLEAN_RE = re.compile(r"[R$€£%\s ]")


def detect_decimal_comma(values: Sequence[str]) -> bool:
    """True se a coluna usa vírgula decimal (padrão brasileiro: 1.234,56)."""
    return any(re.search(r"\d,\d{1,4}$", v.strip()) and not re.fullmatch(r"\d{1,3}(,\d{3})+", v.strip()) for v in values if v)


def parse_number(text: str, decimal_comma: bool) -> Optional[float]:
    raw = _NUM_CLEAN_RE.sub("", str(text or ""))
    if not raw:
        return None
    negative = raw.startswith("(") and raw.endswith(")")
    raw = raw.strip("()")
    raw = raw.replace(".", "").replace(",", ".") if decimal_comma else raw.replace(",", "")
    if not re.fullmatch(r"[-+]?\d+(\.\d+)?([eE][-+]?\d+)?", raw):
        return None
    value = float(raw)
    return -value if negative else value


def parse_date(text: str) -> Optional[date]:
    raw = str(text or "").strip()
    if not raw or not raw[0].isdigit():
        return None
    for pattern in DATE_FORMATS:
        try:
            return datetime.strptime(raw[:19], pattern).date()
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------- perfil das colunas
def _quartiles(values: List[float]) -> Tuple[float, float, float]:
    if len(values) < 4:
        mid = statistics.median(values)
        return min(values), mid, max(values)
    q1, q2, q3 = statistics.quantiles(values, n=4, method="inclusive")
    return q1, q2, q3


def profile_columns(table: Table) -> List[Dict[str, Any]]:
    n_rows = len(table.rows)
    columns: List[Dict[str, Any]] = []
    for idx, name in enumerate(table.headers):
        raw = table.column(idx)
        filled = [v for v in raw if v != ""]
        info: Dict[str, Any] = {"index": idx, "name": name, "missing": n_rows - len(filled),
                                "missing_share": (n_rows - len(filled)) / n_rows if n_rows else 0.0,
                                "unique": len(set(filled))}
        decimal_comma = detect_decimal_comma(filled[:2000])
        numbers = [parse_number(v, decimal_comma) for v in filled]
        num_ok = [x for x in numbers if x is not None]
        dates = [parse_date(v) for v in filled[:3000]]
        date_ok = [d for d in dates if d is not None]
        folded_name = fold(name).replace(" ", "_")
        looks_like_id = bool(ID_NAME_RE.search(folded_name)) and info["unique"] >= 0.5 * max(1, len(filled))
        if filled and len(date_ok) >= 0.85 * len(dates) and not (len(num_ok) >= 0.85 * len(filled)):
            info.update(kind="data", values=[parse_date(v) for v in raw], min=min(date_ok), max=max(date_ok))
        elif filled and len(num_ok) >= 0.85 * len(filled) and not looks_like_id:
            q1, q2, q3 = _quartiles(num_ok)
            info.update(
                kind="numero", decimal_comma=decimal_comma,
                values=[parse_number(v, decimal_comma) if v != "" else None for v in raw],
                count=len(num_ok), sum=math.fsum(num_ok), mean=statistics.fmean(num_ok), median=q2,
                stdev=statistics.stdev(num_ok) if len(num_ok) > 1 else 0.0, min=min(num_ok), max=max(num_ok), q1=q1, q3=q3)
        elif filled and 1 < info["unique"] <= max(2, min(300, int(0.6 * len(filled)))):
            info.update(kind="categoria", values=raw)
        else:
            info.update(kind="identificador" if looks_like_id else "texto", values=raw)
        columns.append(info)
    return columns


def pearson(xs: Sequence[Optional[float]], ys: Sequence[Optional[float]]) -> Optional[Tuple[float, int]]:
    pairs = [(x, y) for x, y in zip(xs, ys) if x is not None and y is not None]
    if len(pairs) < 8:
        return None
    ax, ay = [p[0] for p in pairs], [p[1] for p in pairs]
    if len(set(ax)) < 2 or len(set(ay)) < 2:
        return None
    try:
        return statistics.correlation(ax, ay), len(pairs)
    except statistics.StatisticsError:
        return None


def _group(keys: Sequence[str], values: Sequence[Optional[float]]) -> List[Dict[str, Any]]:
    acc: Dict[str, List[float]] = {}
    for key, value in zip(keys, values):
        if value is None:
            continue
        acc.setdefault(key if key != "" else "(vazio)", []).append(value)
    groups = [{"label": k, "sum": math.fsum(v), "count": len(v), "mean": statistics.fmean(v)} for k, v in acc.items()]
    groups.sort(key=lambda g: g["sum"], reverse=True)
    return groups


def analyze_table(table: Table, measure: Optional[str] = None) -> Dict[str, Any]:
    """Tabela -> estatísticas, cortes, tendência, visão de clientes, qualidade e a lista de FATOS numerados."""
    columns = profile_columns(table)
    numeric = [c for c in columns if c["kind"] == "numero"]
    categorical = [c for c in columns if c["kind"] == "categoria"]
    dates = [c for c in columns if c["kind"] == "data"]
    n_rows = len(table.rows)
    facts: List[str] = []

    def fact(text: str) -> None:
        facts.append(text)

    main: Optional[Dict[str, Any]] = None
    if measure:
        main = next((c for c in numeric if fold(c["name"]) == fold(measure)), None)
        if main is None:
            raise ValueError(f"Coluna numérica '{measure}' não encontrada. Colunas numéricas: {', '.join(c['name'] for c in numeric) or 'nenhuma'}.")
    if main is None:
        main = next((c for c in numeric if MEASURE_NAME_RE.search(fold(c["name"]))), None) or (numeric[0] if numeric else None)

    fact(f"A base '{table.source}' tem {fmt_num(n_rows)} registros e {len(columns)} colunas.")
    result: Dict[str, Any] = {"source": table.source, "rows": n_rows, "columns": columns, "measure": main["name"] if main else None,
                              "breakdowns": [], "trend": None, "customers": None, "correlations": [], "outliers": None}

    if main:
        m = main["name"]
        fact(f"O total de {m} é {fmt_num(main['sum'], 2)}, com média de {fmt_num(main['mean'], 2)} e mediana de {fmt_num(main['median'], 2)} por registro.")
        fact(f"{m} varia de {fmt_num(main['min'], 2)} a {fmt_num(main['max'], 2)} (desvio padrão {fmt_num(main['stdev'], 2)}).")
        iqr = main["q3"] - main["q1"]
        if iqr > 0:
            limit = main["q3"] + 1.5 * iqr
            high = sorted((v for v in main["values"] if v is not None and v > limit), reverse=True)
            if high:
                result["outliers"] = {"limit": limit, "count": len(high), "top": high[:3]}
                fact(f"Há {fmt_num(len(high))} registros com {m} atipicamente alto (acima de {fmt_num(limit, 2)}); o maior é {fmt_num(high[0], 2)}.")

        # cortes por categoria (a coluna de clientes tem seção própria, mais abaixo)
        entity = next((c for c in columns if c["kind"] in {"categoria", "texto", "identificador"} and ENTITY_NAME_RE.search(fold(c["name"]))), None)
        for cat in [c for c in categorical if c is not entity][:4]:
            groups = _group(cat["values"], main["values"])
            if len(groups) < 2:
                continue
            total = math.fsum(g["sum"] for g in groups) or 1.0
            for g in groups:
                g["share"] = g["sum"] / total
            result["breakdowns"].append({"by": cat["name"], "measure": m, "groups": groups[:12], "group_count": len(groups)})
            top = groups[0]
            fact(f"Em '{cat['name']}', '{top['label']}' lidera com {fmt_num(top['sum'], 2)} de {m} ({fmt_pct(top['share'])} do total), entre {fmt_num(len(groups))} grupos.")
            if len(groups) >= 3:
                low = groups[-1]
                fact(f"Em '{cat['name']}', o menor grupo é '{low['label']}' com {fmt_num(low['sum'], 2)} ({fmt_pct(low['share'])}).")

        # visão de clientes (ou a entidade que a base usar)
        if entity is not None and entity["unique"] >= 3:
            groups = _group(entity["values"], main["values"])
            total = math.fsum(g["sum"] for g in groups) or 1.0
            for g in groups:
                g["share"] = g["sum"] / total
            top_n = max(1, math.ceil(len(groups) * 0.2))
            top_share = math.fsum(g["sum"] for g in groups[:top_n]) / total
            single = sum(1 for g in groups if g["count"] == 1)
            customers: Dict[str, Any] = {"by": entity["name"], "count": len(groups), "top": groups[:10], "top20_count": top_n,
                                         "top20_share": top_share, "single_purchase": single, "inactive": []}
            fact(f"A base tem {fmt_num(len(groups))} valores distintos de '{entity['name']}'; a média de {m} por '{entity['name']}' é {fmt_num(total / len(groups), 2)}.")
            fact(f"Os {fmt_num(top_n)} maiores em '{entity['name']}' (20% do total) concentram {fmt_pct(top_share)} de {m}.")
            fact(f"O maior em '{entity['name']}' é '{groups[0]['label']}', com {fmt_num(groups[0]['sum'], 2)} ({fmt_pct(groups[0]['share'])}) em {fmt_num(groups[0]['count'])} registros.")
            if single:
                fact(f"{fmt_num(single)} de {fmt_num(len(groups))} em '{entity['name']}' ({fmt_pct(single / len(groups))}) aparecem em um único registro.")
            if dates:
                last_seen: Dict[str, date] = {}
                for key, when in zip(entity["values"], dates[0]["values"]):
                    if when is not None and (key not in last_seen or when > last_seen[key]):
                        last_seen[key] = when
                if last_seen:
                    reference = max(last_seen.values())
                    cutoff = reference - timedelta(days=90)
                    value_of = {g["label"]: g["sum"] for g in groups}
                    inactive = sorted(((k, d) for k, d in last_seen.items() if d < cutoff), key=lambda kd: value_of.get(kd[0], 0.0), reverse=True)
                    customers["inactive"] = [{"label": k, "last": d.isoformat(), "sum": value_of.get(k, 0.0)} for k, d in inactive[:10]]
                    customers["inactive_count"] = len(inactive)
                    if inactive:
                        lost = math.fsum(value_of.get(k, 0.0) for k, _ in inactive)
                        fact(f"{fmt_num(len(inactive))} em '{entity['name']}' não têm registro nos 90 dias anteriores a {reference.strftime('%d/%m/%Y')}; "
                             f"juntos somaram {fmt_num(lost, 2)} de {m} ({fmt_pct(lost / total)}) no período analisado.")
            result["customers"] = customers

        # tendência mensal
        if dates:
            months: Dict[str, float] = {}
            for when, value in zip(dates[0]["values"], main["values"]):
                if when is not None and value is not None:
                    key = f"{when.year:04d}-{when.month:02d}"
                    months[key] = months.get(key, 0.0) + value
            series = sorted(months.items())[-18:]
            if len(series) >= 2:
                result["trend"] = {"by": dates[0]["name"], "measure": m, "points": [{"label": k, "sum": v} for k, v in series]}
                best = max(series, key=lambda kv: kv[1])
                fact(f"O melhor mês em {m} foi {best[0]}, com {fmt_num(best[1], 2)}.")
                (prev_k, prev_v), (last_k, last_v) = series[-2], series[-1]
                if prev_v:
                    fact(f"No último mês da base ({last_k}), {m} foi {fmt_num(last_v, 2)}: variação de {fmt_pct(last_v / prev_v - 1)} sobre {prev_k}.")
                first_k, first_v = series[0]
                if first_v and len(series) >= 4:
                    fact(f"Entre {first_k} e {last_k}, {m} mensal passou de {fmt_num(first_v, 2)} para {fmt_num(last_v, 2)} ({fmt_pct(last_v / first_v - 1)}).")

    # correlações
    for i, a in enumerate(numeric[:8]):
        for b in numeric[i + 1:8]:
            found = pearson(a["values"], b["values"])
            if found and abs(found[0]) >= 0.3:
                result["correlations"].append({"a": a["name"], "b": b["name"], "r": found[0], "n": found[1]})
    result["correlations"].sort(key=lambda c: abs(c["r"]), reverse=True)
    for corr in result["correlations"][:3]:
        strength = "forte" if abs(corr["r"]) >= 0.7 else "moderada"
        direction = "positiva" if corr["r"] > 0 else "negativa"
        fact(f"Correlação {strength} {direction} entre '{corr['a']}' e '{corr['b']}' (r = {fmt_num(corr['r'], 2)}, {fmt_num(corr['n'])} pares). Correlação não indica causa.")

    # qualidade
    duplicates = n_rows - len({tuple(r) for r in table.rows})
    worst = max(columns, key=lambda c: c["missing_share"]) if columns else None
    result["quality"] = {"duplicates": duplicates, "missing": [{"name": c["name"], "share": c["missing_share"]} for c in columns if c["missing"]]}
    if duplicates:
        fact(f"Há {fmt_num(duplicates)} linhas duplicadas ({fmt_pct(duplicates / n_rows)}).")
    if worst and worst["missing"]:
        fact(f"A coluna com mais valores vazios é '{worst['name']}' ({fmt_pct(worst['missing_share'])}).")
    result["facts"] = facts
    return result


# ---------------------------------------------------------------- conferência do texto da IA
_NUMBER_RE = re.compile(r"(?<![\w/])-?\d[\d.,]*\d|(?<![\w/])\d")


def _numbers_in(text: str, with_tokens: bool = False) -> List[Any]:
    found: List[Any] = []
    for token in _NUMBER_RE.findall(str(text or "")):
        token = token.strip(".,")
        if not token:
            continue
        if "," in token:
            normalized = token.replace(".", "").replace(",", ".")
        elif re.fullmatch(r"-?\d{1,3}(\.\d{3})+", token):
            normalized = token.replace(".", "")
        else:
            normalized = token
        try:
            found.append((float(normalized), token) if with_tokens else float(normalized))
        except ValueError:
            continue
    return found


def unsupported_numbers(narrative: str, facts: Sequence[str]) -> List[str]:
    """Números do texto da IA que não aparecem nos fatos calculados (tolerância de arredondamento).
    Ignora referências [n] aos fatos e inteiros pequenos (contagens de itens, 'top 3', etc.)."""
    known = _numbers_in(" ".join(facts))
    text = re.sub(r"\[\s*(?:fato\s*)?\d+(?:\s*[,e]\s*\d+)*\s*\]", " ", str(narrative or ""), flags=re.IGNORECASE)
    text = re.sub(r"(?m)^\s*\d+[.)]\s", " ", text)
    suspicious: List[str] = []
    for value, token in _numbers_in(text, with_tokens=True):
        if float(value).is_integer() and abs(value) <= 20:
            continue
        if any(abs(value - k) <= max(0.051, abs(k) * 0.006) for k in known):
            continue
        if token not in suspicious:
            suspicious.append(token)              # como a IA escreveu, para ser fácil de achar no texto
    return suspicious


# ---------------------------------------------------------------- gráficos (SVG)
def _esc(text: Any) -> str:
    return html.escape(str(text if text is not None else ""), quote=True)


def _cap(text: str) -> str:
    text = str(text)
    return text[:1].upper() + text[1:]


def _short(text: str, limit: int) -> str:
    text = str(text)
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


_MONTHS_PT = ("jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez")


def month_label(label: str) -> str:
    """'2025-09' -> 'set/25'; outros rótulos voltam como vieram."""
    m = re.fullmatch(r"(\d{4})-(\d{2})", str(label).strip())
    if not m or not 1 <= int(m.group(2)) <= 12:
        return str(label)
    return f"{_MONTHS_PT[int(m.group(2)) - 1]}/{m.group(1)[2:]}"


def fmt_compact(value: Optional[float]) -> str:
    """Número curto para cartões de slide: 2.838.332,20 -> '2,84 mi'; 7.430,19 -> '7.430'."""
    if value is None:
        return "—"
    v = float(value)
    for limit, suffix in ((1e9, " bi"), (1e6, " mi")):
        if abs(v) >= limit:
            return fmt_num(v / limit, 2) + suffix
    return fmt_num(v, 0 if abs(v) >= 1000 else (2 if v != int(v) else 0))


def svg_bars(items: Sequence[Tuple[str, float]], title: str, decimals: Optional[int] = 2, scale: float = 1.0, show_title: bool = True) -> str:
    """Barras horizontais de uma única série: barra fina, ponta arredondada, valor na ponta em tinta de texto."""
    items = [(str(label), max(0.0, float(value))) for label, value in items][:12]
    if not items:
        return ""
    label_w, bar_max, value_w, row_h, bar_h, top = 190, 330, 110, 28, 16, (34 if show_title else 8)
    width, height = label_w + bar_max + value_w, top + row_h * len(items) + 8
    peak = max(v for _, v in items) or 1.0
    parts = [f'<svg viewBox="0 0 {width} {height}" width="100%" role="img" aria-label="{_esc(title)}" xmlns="http://www.w3.org/2000/svg" '
             f'style="max-width:{int(width * scale)}px;font-family:inherit">',
             (f'<text x="0" y="16" font-size="13" font-weight="600" fill="{INK}">{_esc(title)}</text>' if show_title else ""),
             f'<line x1="{label_w}" y1="{top - 4}" x2="{label_w}" y2="{height - 6}" stroke="{RULE}" stroke-width="1"/>']
    for idx, (label, value) in enumerate(items):
        y = top + idx * row_h
        w = max(2.0, bar_max * value / peak)
        r = min(4.0, w / 2)
        path = (f"M{label_w},{y} h{w - r:.1f} a{r},{r} 0 0 1 {r},{r} v{bar_h - 2 * r} a{r},{r} 0 0 1 -{r},{r} h-{w - r:.1f} z")
        shown = fmt_num(value, decimals)
        parts.append(f'<g><title>{_esc(label)}: {shown}</title>'
                     f'<text x="{label_w - 8}" y="{y + 12}" font-size="12" text-anchor="end" fill="{INK_2}">{_esc(_short(label, 28))}</text>'
                     f'<path d="{path}" fill="{SERIES}"/>'
                     f'<text x="{label_w + w + 6:.1f}" y="{y + 12}" font-size="12" fill="{INK}">{shown}</text></g>')
    parts.append("</svg>")
    return "".join(parts)


def svg_columns(points: Sequence[Tuple[str, float]], title: str, scale: float = 1.0, show_title: bool = True) -> str:
    """Colunas por período (série única), base comum em zero, rótulo só no primeiro, no maior e no último."""
    points = [(str(k), max(0.0, float(v))) for k, v in points][-18:]
    if len(points) < 2:
        return ""
    width, height, left, bottom, top = 640, 250, 8, 36, 40
    plot_h = height - bottom - top
    slot = (width - 2 * left) / len(points)
    col_w = min(24.0, slot - 4)
    peak = max(v for _, v in points) or 1.0
    best = max(range(len(points)), key=lambda i: points[i][1])
    parts = [f'<svg viewBox="0 0 {width} {height}" width="100%" role="img" aria-label="{_esc(title)}" xmlns="http://www.w3.org/2000/svg" '
             f'style="max-width:{int(width * scale)}px;font-family:inherit">',
             (f'<text x="0" y="16" font-size="13" font-weight="600" fill="{INK}">{_esc(title)}</text>' if show_title else ""),
             f'<line x1="{left}" y1="{height - bottom}" x2="{width - left}" y2="{height - bottom}" stroke="{RULE}" stroke-width="1"/>']
    for idx, (label, value) in enumerate(points):
        h = max(2.0, plot_h * value / peak)
        x = left + idx * slot + (slot - col_w) / 2
        y = height - bottom - h
        r = min(4.0, col_w / 2, h / 2)
        path = (f"M{x:.1f},{height - bottom} v-{h - r:.1f} a{r},{r} 0 0 1 {r},-{r} h{col_w - 2 * r:.1f} a{r},{r} 0 0 1 {r},{r} v{h - r:.1f} z")
        parts.append(f'<g><title>{_esc(label)}: {fmt_num(value, 2)}</title><path d="{path}" fill="{SERIES}"/>')
        if idx in {0, best, len(points) - 1}:
            parts.append(f'<text x="{x + col_w / 2:.1f}" y="{y - 6:.1f}" font-size="11" text-anchor="middle" fill="{INK}">{fmt_num(value, 0)}</text>')
        if len(points) <= 9 or idx % 2 == 0 or idx == len(points) - 1:
            parts.append(f'<text x="{x + col_w / 2:.1f}" y="{height - bottom + 16}" font-size="10.5" text-anchor="middle" fill="{INK_2}">{_esc(_short(month_label(label), 10))}</text>')
        parts.append("</g>")
    parts.append("</svg>")
    return "".join(parts)


# ---------------------------------------------------------------- documentos
_DOC_CSS = f"""
*{{box-sizing:border-box}} body{{margin:0;background:#f4f4f2;color:{INK};font:15px/1.55 -apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}}
main{{max-width:880px;margin:0 auto;padding:40px 28px 64px;background:{SURFACE}}}
h1{{font-size:26px;line-height:1.25;margin:0 0 6px}} h2{{font-size:17px;margin:34px 0 10px;padding-top:18px;border-top:1px solid {RULE}}}
.meta{{color:{INK_2};font-size:13px;margin-bottom:22px}} p{{margin:8px 0}} ol,ul{{margin:8px 0;padding-left:22px}} li{{margin:5px 0}}
table{{border-collapse:collapse;width:100%;font-size:13px;margin:10px 0}} th,td{{text-align:left;padding:6px 8px;border-bottom:1px solid {RULE}}}
th{{color:{INK_2};font-weight:600}} td.n,th.n{{text-align:right;font-variant-numeric:tabular-nums}}
.tiles{{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin:14px 0}}
.tile{{border:1px solid {RULE};border-radius:8px;padding:12px 14px;background:#fff}} .tile b{{display:block;font-size:22px;line-height:1.2}}
.tile span{{color:{INK_2};font-size:12px}} figure{{margin:18px 0}} .note{{color:{INK_2};font-size:12.5px}}
.ai{{border-left:3px solid {SERIES};padding:2px 0 2px 14px;margin:10px 0}} .warn{{border:1px solid #e0b400;background:#fff9e0;border-radius:6px;padding:9px 12px;font-size:13px}}
@media print{{body{{background:#fff}} main{{padding:0}} h2{{break-after:avoid}} figure,table{{break-inside:avoid}}}}
"""


def _narrative_html(text: str) -> str:
    """Texto simples da LLM -> HTML seguro (tudo escapado; listas por linha)."""
    blocks: List[str] = []
    items: List[str] = []

    def flush() -> None:
        if items:
            blocks.append("<ul>" + "".join(f"<li>{i}</li>" for i in items) + "</ul>")
            items.clear()

    for raw in str(text or "").splitlines():
        line = raw.strip()
        if not line:
            flush()
            continue
        line = re.sub(r"[*_`#]+", "", line).strip()
        bullet = re.match(r"^(?:[-•–]|\d+[.)])\s+(.*)$", line)
        if bullet:
            items.append(_esc(bullet.group(1)))
        elif line.endswith(":") and len(line) < 80:
            flush()
            blocks.append(f"<p><b>{_esc(line)}</b></p>")
        else:
            flush()
            blocks.append(f"<p>{_esc(line)}</p>")
    flush()
    return "".join(blocks)


def _distinct_label(column: str) -> str:
    name = str(column).strip()
    if name[-1:].lower() in "aeiou" and " " not in name:
        return f"{name}s distintos"
    return f"valores distintos de {name}"


def _tiles(analysis: Dict[str, Any]) -> List[Tuple[str, str]]:
    tiles = [(fmt_num(analysis["rows"]), "registros")]
    main = next((c for c in analysis["columns"] if c["name"] == analysis.get("measure")), None)
    if main:
        tiles += [(fmt_num(main["sum"], 2), f"total de {main['name']}"), (fmt_num(main["mean"], 2), "média por registro")]
    if analysis.get("customers"):
        tiles += [(fmt_num(analysis["customers"]["count"]), _distinct_label(analysis["customers"]["by"])),
                  (fmt_pct(analysis["customers"]["top20_share"]), "concentração nos 20% maiores")]
    return tiles


def render_report_html(title: str, analysis: Dict[str, Any], narrative: str = "", question: str = "", flagged: Sequence[str] = ()) -> str:
    out = [f'<!doctype html><html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
           f"<title>{_esc(title)}</title><style>{_DOC_CSS}</style></head><body><main>",
           f"<h1>{_esc(title)}</h1>",
           f'<div class="meta">Fonte: {_esc(analysis["source"])} · {fmt_num(analysis["rows"])} registros · gerado em {datetime.now().strftime("%d/%m/%Y %H:%M")}</div>']
    if question:
        out.append(f"<p><b>Pergunta:</b> {_esc(question)}</p>")
    out.append('<div class="tiles">' + "".join(f'<div class="tile"><b>{_esc(v)}</b><span>{_esc(l)}</span></div>' for v, l in _tiles(analysis)) + "</div>")

    out.append("<h2>Fatos calculados</h2><ol>" + "".join(f"<li>{_esc(f)}</li>" for f in analysis["facts"]) + "</ol>")
    out.append('<p class="note">Todos os números acima foram calculados diretamente da base, sem uso de IA.</p>')

    if narrative.strip():
        out.append("<h2>Interpretação e recomendações</h2>")
        out.append('<p class="note">Texto redigido por IA a partir dos fatos numerados acima. Revise antes de usar em decisões.</p>')
        if flagged:
            out.append(f'<p class="warn"><b>Conferir:</b> o texto abaixo cita número(s) que não constam nos fatos calculados: {_esc(", ".join(flagged))}.</p>')
        out.append(f'<div class="ai">{_narrative_html(narrative)}</div>')

    for cut in analysis["breakdowns"]:
        out.append(f"<h2>{_esc(_cap(cut['measure']))} por {_esc(cut['by'])}</h2>")
        out.append("<figure>" + svg_bars([(g["label"], g["sum"]) for g in cut["groups"][:10]], f"Soma de {cut['measure']} por {cut['by']}") + "</figure>")
        out.append('<table><tr><th>' + _esc(cut["by"]) + '</th><th class="n">Soma</th><th class="n">Participação</th><th class="n">Registros</th><th class="n">Média</th></tr>'
                   + "".join(f'<tr><td>{_esc(g["label"])}</td><td class="n">{fmt_num(g["sum"], 2)}</td><td class="n">{fmt_pct(g["share"])}</td>'
                             f'<td class="n">{fmt_num(g["count"])}</td><td class="n">{fmt_num(g["mean"], 2)}</td></tr>' for g in cut["groups"]) + "</table>")
        if cut["group_count"] > len(cut["groups"]):
            out.append(f'<p class="note">Exibindo os {len(cut["groups"])} maiores de {cut["group_count"]} grupos.</p>')

    if analysis.get("trend"):
        trend = analysis["trend"]
        out.append(f"<h2>Evolução mensal de {_esc(trend['measure'])}</h2><figure>"
                   + svg_columns([(p["label"], p["sum"]) for p in trend["points"]], f"Soma de {trend['measure']} por mês") + "</figure>")
        out.append('<table><tr><th>Mês</th><th class="n">Soma</th></tr>'
                   + "".join(f'<tr><td>{_esc(p["label"])}</td><td class="n">{fmt_num(p["sum"], 2)}</td></tr>' for p in trend["points"]) + "</table>")

    customers = analysis.get("customers")
    if customers:
        out.append(f"<h2>Visão por {_esc(customers['by'])}</h2>")
        out.append("<figure>" + svg_bars([(g["label"], g["sum"]) for g in customers["top"]], f"Maiores em {customers['by']} (soma de {analysis['measure']})") + "</figure>")
        if customers.get("inactive"):
            out.append(f"<p><b>Sem registro nos últimos 90 dias da base</b> ({fmt_num(customers.get('inactive_count', 0))} no total; os de maior valor histórico):</p>")
            out.append('<table><tr><th>' + _esc(customers["by"]) + '</th><th>Último registro</th><th class="n">Valor no período</th></tr>'
                       + "".join(f'<tr><td>{_esc(i["label"])}</td><td>{_esc(i["last"])}</td><td class="n">{fmt_num(i["sum"], 2)}</td></tr>' for i in customers["inactive"]) + "</table>")

    numeric = [c for c in analysis["columns"] if c["kind"] == "numero"]
    if numeric:
        out.append('<h2>Estatísticas das colunas numéricas</h2><table><tr><th>Coluna</th><th class="n">Média</th><th class="n">Mediana</th>'
                   '<th class="n">Desvio padrão</th><th class="n">Mínimo</th><th class="n">Máximo</th><th class="n">Vazios</th></tr>'
                   + "".join(f'<tr><td>{_esc(c["name"])}</td><td class="n">{fmt_num(c["mean"], 2)}</td><td class="n">{fmt_num(c["median"], 2)}</td>'
                             f'<td class="n">{fmt_num(c["stdev"], 2)}</td><td class="n">{fmt_num(c["min"], 2)}</td><td class="n">{fmt_num(c["max"], 2)}</td>'
                             f'<td class="n">{fmt_num(c["missing"])}</td></tr>' for c in numeric) + "</table>")
    if analysis["correlations"]:
        out.append('<h2>Correlações</h2><table><tr><th>Par de colunas</th><th class="n">r de Pearson</th><th class="n">Pares</th></tr>'
                   + "".join(f'<tr><td>{_esc(c["a"])} × {_esc(c["b"])}</td><td class="n">{fmt_num(c["r"], 2)}</td><td class="n">{fmt_num(c["n"])}</td></tr>'
                             for c in analysis["correlations"][:10]) + '</table><p class="note">Correlação mede associação linear; não indica causa.</p>')
    quality = analysis["quality"]
    out.append("<h2>Qualidade dos dados</h2><ul>"
               + f"<li>Linhas duplicadas: {fmt_num(quality['duplicates'])}</li>"
               + ("".join(f"<li>'{_esc(m['name'])}': {fmt_pct(m['share'])} de valores vazios</li>" for m in quality["missing"]) or "<li>Nenhuma coluna com valores vazios.</li>")
               + "</ul>")
    out.append('<p class="note">Método: estatísticas descritivas (média, mediana, desvio padrão amostral, quartis), somas por grupo, '
               "valores atípicos pelo critério de 1,5 × intervalo interquartil e correlação de Pearson.</p></main></body></html>")
    return "".join(out)


_DECK_CSS = f"""
*{{box-sizing:border-box}} html{{scroll-snap-type:y mandatory}} body{{margin:0;background:#e9e9e6;color:{INK};font:18px/1.5 -apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}}
section{{scroll-snap-align:start;min-height:100vh;display:flex;flex-direction:column;padding:6vh 8vw 3vh;background:{SURFACE};border-bottom:1px solid {RULE};overflow-wrap:anywhere}}
section .content{{flex:1;display:flex;flex-direction:column;justify-content:center;gap:22px;min-width:0}} section .content svg{{align-self:center}}
section.cover{{background:#0d366b;color:#fff;justify-content:center}} section.cover p{{color:#cde2fb;margin:0}}
h1{{font-size:clamp(30px,4.4vw,48px);line-height:1.15;margin:0 0 14px;text-wrap:balance}} h2{{font-size:clamp(22px,2.6vw,32px);line-height:1.2;margin:0;text-wrap:balance}}
h2::after{{content:"";display:block;width:48px;height:4px;border-radius:2px;background:{SERIES};margin-top:12px}}
ul{{margin:0;padding-left:26px;max-width:60em}} li{{margin:10px 0}} li::marker{{color:{SERIES}}}
.foot{{display:flex;justify-content:space-between;gap:16px;padding-top:18px;color:{INK_2};font-size:13px}}
.tiles{{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,180px),1fr));gap:16px}}
.tile{{border:1px solid {RULE};border-radius:10px;padding:18px 20px;background:#fff;min-width:0}}
.tile b{{display:block;font-size:clamp(24px,2.6vw,34px);line-height:1.15;font-variant-numeric:tabular-nums;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
.tile span{{color:{INK_2};font-size:14px}} .tile small{{display:block;color:{INK_2};font-size:12px;margin-top:2px}}
@media (max-width:640px){{section{{padding:5vh 16px 2vh}} body{{font-size:16px}}}}
@media print{{html{{scroll-snap-type:none}} section{{min-height:auto;height:100vh;break-after:page}}}}
"""


def _deck_tiles(analysis: Dict[str, Any]) -> List[Tuple[str, str]]:
    """Como _tiles, mas com números grandes abreviados (2,84 mi) e o valor exato na legenda."""
    out = []
    for value, label in _tiles(analysis):
        number = parse_number(value.replace("%", ""), True) if "%" not in value else None
        short = fmt_compact(number) if number is not None and abs(number) >= 1e6 else value
        out.append((short, label if short == value else f"{label} ({value})"))
    return out


def build_slides(title: str, analysis: Dict[str, Any], narrative: str = "") -> List[Dict[str, Any]]:
    """Roteiro da apresentação a partir da análise: capa, números, um slide por corte, tendência, clientes, conclusões."""
    slides: List[Dict[str, Any]] = [{"kind": "cover", "title": title,
                                     "subtitle": f"Fonte: {analysis['source']} · {fmt_num(analysis['rows'])} registros · {datetime.now().strftime('%d/%m/%Y')}"}]
    slides.append({"kind": "tiles", "title": "Visão geral", "tiles": _deck_tiles(analysis)})
    for cut in analysis["breakdowns"][:3]:
        top = cut["groups"][0]
        slides.append({"kind": "chart", "title": f"{_cap(cut['measure'])} por {cut['by']}",
                       "chart": ("bars", [(g["label"], g["sum"]) for g in cut["groups"][:8]]),
                       "bullets": [f"'{top['label']}' lidera com {fmt_pct(top['share'])} do total."]})
    if analysis.get("trend"):
        slides.append({"kind": "chart", "title": f"Evolução mensal de {analysis['trend']['measure']}",
                       "chart": ("columns", [(p["label"], p["sum"]) for p in analysis["trend"]["points"]]), "bullets": []})
    customers = analysis.get("customers")
    if customers:
        bullets = [f"Os {fmt_num(customers['top20_count'])} maiores (20%) concentram {fmt_pct(customers['top20_share'])} do total."]
        if customers.get("inactive_count"):
            bullets.append(f"{fmt_num(customers['inactive_count'])} sem registro nos últimos 90 dias da base.")
        slides.append({"kind": "chart", "title": f"Maiores em {customers['by']}",
                       "chart": ("bars", [(g["label"], g["sum"]) for g in customers["top"][:8]]), "bullets": bullets})
    lines = [re.sub(r"^(?:[-•–]|\d+[.)])\s+", "", ln.strip()) for ln in str(narrative or "").splitlines()]
    lines = [re.sub(r"[*_`#]+", "", ln).strip() for ln in lines if len(ln.strip()) > 25 and not ln.strip().endswith(":")]
    if lines:
        slides.append({"kind": "bullets", "title": "Interpretação e recomendações", "bullets": lines[:6],
                       "foot": "Texto redigido por IA a partir dos fatos calculados. Revisar antes de decidir."})
    else:
        slides.append({"kind": "bullets", "title": "Principais fatos", "bullets": analysis["facts"][1:7]})
    slides.append({"kind": "bullets", "title": "Próximos passos", "bullets": [
        "Validar os números com a área responsável pela base.",
        "Aprofundar os pontos de maior concentração e os grupos em queda.",
        "Definir responsáveis e prazos para as ações priorizadas."]})
    return slides


def render_deck_html(title: str, slides: Sequence[Dict[str, Any]]) -> str:
    out = [f'<!doctype html><html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
           f"<title>{_esc(title)}</title><style>{_DECK_CSS}</style></head><body>"]
    total = len(slides)
    for idx, slide in enumerate(slides, start=1):
        kind = slide.get("kind", "bullets")
        if kind == "cover":
            out.append(f'<section class="cover"><h1>{_esc(slide["title"])}</h1><p>{_esc(slide.get("subtitle", ""))}</p></section>')
            continue
        out.append(f"<section><h2>{_esc(slide['title'])}</h2><div class=\"content\">")
        if kind == "tiles":
            out.append('<div class="tiles">' + "".join(
                f'<div class="tile"><b title="{_esc(v)}">{_esc(v)}</b><span>{_esc(l)}</span></div>' for v, l in slide["tiles"]) + "</div>")
        if kind == "chart":
            chart_kind, data = slide["chart"]
            out.append(svg_bars(data, slide["title"], scale=1.65, show_title=False) if chart_kind == "bars"
                       else svg_columns(data, slide["title"], scale=1.6, show_title=False))
        bullets = [b for b in slide.get("bullets", []) if str(b).strip()]
        if bullets:
            out.append("<ul>" + "".join(f"<li>{_esc(_short(str(b), 260))}</li>" for b in bullets) + "</ul>")
        out.append(f'</div><div class="foot"><span>{_esc(slide.get("foot", ""))}</span><span>{_esc(_short(title, 60))} · {idx} / {total}</span></div></section>')
    out.append("</body></html>")
    return "".join(out)


def _rgb(hex_color: str) -> Any:
    from pptx.dml.color import RGBColor  # type: ignore
    return RGBColor.from_string(hex_color.lstrip("#").upper())


def write_pptx(path: Path, title: str, slides: Sequence[Dict[str, Any]]) -> bool:
    """Grava um .pptx editável, com o mesmo visual do HTML: capa azul, título alinhado à esquerda com filete, cartões de
    números, gráficos nativos do PowerPoint (eixo formatado, sem título duplicado) e rodapé com página.
    False se python-pptx não estiver instalado."""
    try:
        from pptx import Presentation  # type: ignore
        from pptx.chart.data import CategoryChartData  # type: ignore
        from pptx.enum.chart import XL_CHART_TYPE, XL_LABEL_POSITION  # type: ignore
        from pptx.enum.shapes import MSO_SHAPE  # type: ignore
        from pptx.enum.text import MSO_ANCHOR, PP_ALIGN  # type: ignore
        from pptx.util import Inches, Pt  # type: ignore
    except ImportError:
        return False
    deck = Presentation()
    deck.slide_width, deck.slide_height = Inches(13.333), Inches(7.5)
    blank = deck.slide_layouts[6]
    margin, width = Inches(0.8), Inches(13.333 - 1.6)
    total = len(slides)

    def text_box(page: Any, left: Any, top: Any, w: Any, h: Any, lines: Sequence[str], size: int, color: str = INK,
                 bold: bool = False, bullets: bool = False, anchor: Any = None) -> Any:
        box = page.shapes.add_textbox(left, top, w, h)
        frame = box.text_frame
        frame.word_wrap = True
        if anchor is not None:
            frame.vertical_anchor = anchor
        for i, line in enumerate(lines):
            para = frame.paragraphs[0] if i == 0 else frame.add_paragraph()
            if bullets:
                run_dot = para.add_run()
                run_dot.text = "•  "
                run_dot.font.color.rgb = _rgb(SERIES)
                run_dot.font.size = Pt(size)
                run_dot.font.bold = True
            run = para.add_run()
            run.text = line
            run.font.size, run.font.bold, run.font.color.rgb = Pt(size), bold, _rgb(color)
            para.space_after = Pt(size * 0.6)
        return box

    for idx, slide in enumerate(slides, start=1):
        kind = slide.get("kind", "bullets")
        page = deck.slides.add_slide(blank)
        if kind == "cover":
            page.background.fill.solid()
            page.background.fill.fore_color.rgb = _rgb("#0d366b")
            text_box(page, margin, Inches(2.4), width, Inches(1.8), [str(slide["title"])[:200]], 40, "#ffffff", True,
                     anchor=MSO_ANCHOR.BOTTOM)
            text_box(page, margin, Inches(4.35), width, Inches(1.0), [str(slide.get("subtitle", ""))[:300]], 16, "#cde2fb")
            continue
        page.background.fill.solid()
        page.background.fill.fore_color.rgb = _rgb(SURFACE)
        heading = str(slide["title"])[:200]
        text_box(page, margin, Inches(0.45), width, Inches(1.0), [heading], 26 if len(heading) <= 60 else 20, INK, True,
                 anchor=MSO_ANCHOR.BOTTOM)
        rule = page.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, margin, Inches(1.55), Inches(0.55), Inches(0.06))
        rule.fill.solid()
        rule.fill.fore_color.rgb = _rgb(SERIES)
        rule.line.fill.background()
        bullets = [_short(str(b), 240) for b in slide.get("bullets", []) if str(b).strip()]
        top = Inches(1.9)
        if kind == "tiles":
            tiles = list(slide["tiles"])[:6]
            gap = Inches(0.25)
            tile_w = int((width - gap * (len(tiles) - 1)) / max(1, len(tiles)))
            for t, (value, label) in enumerate(tiles):
                card = page.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, margin + t * (tile_w + gap), Inches(2.4), tile_w, Inches(1.9))
                card.adjustments[0] = 0.08
                card.fill.solid()
                card.fill.fore_color.rgb = _rgb("#ffffff")
                card.line.color.rgb = _rgb(RULE)
                frame = card.text_frame
                frame.word_wrap, frame.vertical_anchor = True, MSO_ANCHOR.TOP
                frame.margin_left = frame.margin_right = Inches(0.18)
                frame.margin_top = Inches(0.2)
                p1 = frame.paragraphs[0]
                p1.alignment = PP_ALIGN.LEFT
                r1 = p1.add_run()
                r1.text = str(value)
                r1.font.size, r1.font.bold, r1.font.color.rgb = Pt(28 if len(tiles) <= 4 else 24), True, _rgb(INK)
                p2 = frame.add_paragraph()
                r2 = p2.add_run()
                r2.text = str(label)
                r2.font.size, r2.font.color.rgb = Pt(12), _rgb(INK_2)
            top = Inches(4.7)
        if kind == "chart":
            chart_kind, data = slide["chart"]
            data = list(data)
            if chart_kind == "bars":
                data = data[::-1]                                  # o PowerPoint desenha de baixo para cima: maior no topo
            chart_data = CategoryChartData(number_format="#,##0")
            chart_data.categories = [_short(month_label(str(label)), 40) for label, _ in data]
            chart_data.add_series(str(slide["title"])[:60], [float(value) for _, value in data])
            chart_type = XL_CHART_TYPE.BAR_CLUSTERED if chart_kind == "bars" else XL_CHART_TYPE.COLUMN_CLUSTERED
            chart_h = Inches(4.0 if bullets else 4.9)
            frame = page.shapes.add_chart(chart_type, margin, Inches(1.85), width, chart_h, chart_data)
            chart = frame.chart
            chart.has_legend, chart.has_title = False, False
            chart.font.size, chart.font.color.rgb = Pt(12), _rgb(INK_2)
            plot = chart.plots[0]
            plot.gap_width = 60
            plot.has_data_labels = True
            labels = plot.data_labels
            labels.number_format, labels.number_format_is_linked = "#,##0", False
            labels.position = XL_LABEL_POSITION.OUTSIDE_END
            labels.font.size, labels.font.color.rgb = Pt(11), _rgb(INK)
            series = plot.series[0]
            series.format.fill.solid()
            series.format.fill.fore_color.rgb = _rgb(SERIES)
            value_axis = chart.value_axis
            value_axis.has_major_gridlines = False
            value_axis.visible = False
            value_axis.tick_labels.number_format, value_axis.tick_labels.number_format_is_linked = "#,##0", False
            chart.category_axis.format.line.color.rgb = _rgb(RULE)
            top = Inches(1.85) + chart_h + Inches(0.1)
        if bullets:
            text_box(page, margin, top, width, Inches(6.75) - top, bullets, 22 if kind == "bullets" else 16, INK, bullets=True)
        text_box(page, margin, Inches(6.85), Inches(8.5), Inches(0.4), [str(slide.get("foot", ""))[:200]], 10, INK_2)
        text_box(page, Inches(13.333 - 0.8 - 3.6), Inches(6.85), Inches(3.6), Inches(0.4),
                 [f"{_short(title, 40)} · {idx} / {total}"], 10, INK_2).text_frame.paragraphs[0].alignment = PP_ALIGN.RIGHT
    path.parent.mkdir(parents=True, exist_ok=True)
    deck.save(str(path))
    return True


def write_xlsx(path: Path, table: "Table", analysis: Dict[str, Any]) -> bool:
    """Planilha auditável: aba Dados (valores tipados), aba Resumo (fatos) e uma aba por corte com FÓRMULAS
    (SOMASES/CONT.SES/MÉDIASES) apontando para a aba Dados — quem recebe confere cada número no próprio Excel.
    False se openpyxl não estiver instalado."""
    try:
        import openpyxl  # type: ignore
        from openpyxl.styles import Font  # type: ignore
        from openpyxl.utils import get_column_letter  # type: ignore
    except ImportError:
        return False
    book = openpyxl.Workbook()
    summary = book.active
    summary.title = "Resumo"
    summary.append(["Fatos calculados", f"Fonte: {analysis['source']}"])
    summary["A1"].font = Font(bold=True)
    for idx, fact in enumerate(analysis["facts"], start=1):
        summary.append([idx, fact])
    summary.column_dimensions["B"].width = 120

    data = book.create_sheet("Dados")
    data.append(table.headers)
    for cell in data[1]:
        cell.font = Font(bold=True)
    cols = analysis["columns"]
    for r_idx in range(len(table.rows)):
        row: List[Any] = []
        for col in cols:
            value = col["values"][r_idx] if col["kind"] in {"numero", "data"} else table.rows[r_idx][col["index"]]
            row.append("" if value is None else value)
        data.append(row)
    last = len(table.rows) + 1
    letter = {c["name"]: get_column_letter(c["index"] + 1) for c in cols}
    measure = analysis.get("measure")
    for cut in analysis["breakdowns"]:
        sheet = book.create_sheet(_short(f"Por {cut['by']}", 31).replace("/", "-"))
        sheet.append([cut["by"], f"Soma de {measure}", "Participação", "Registros", "Média"])
        for cell in sheet[1]:
            cell.font = Font(bold=True)
        cat, val = letter[cut["by"]], letter[measure]
        rng_c, rng_v = f"Dados!${cat}$2:${cat}${last}", f"Dados!${val}$2:${val}${last}"
        n = len(cut["groups"])
        for i, group in enumerate(cut["groups"], start=2):
            sheet.append([group["label"], f"=SUMIFS({rng_v},{rng_c},A{i})", f"=B{i}/SUM({rng_v})",
                          f"=COUNTIFS({rng_c},A{i},{rng_v},\"<>\")", f"=AVERAGEIFS({rng_v},{rng_c},A{i})"])
            sheet[f"C{i}"].number_format = "0.0%"
            for col_l in ("B", "E"):
                sheet[f"{col_l}{i}"].number_format = "#,##0.00"
        sheet.column_dimensions["A"].width = 28
        if cut["group_count"] > n:
            sheet.append([f"(exibindo os {n} maiores de {cut['group_count']} grupos)"])
    path.parent.mkdir(parents=True, exist_ok=True)
    book.save(str(path))
    return True


def read_document_text(path: Path, max_chars: int = 200_000) -> str:
    """Texto de .txt, .md, .csv, .docx (biblioteca padrão) e .pdf (se pypdf estiver instalado)."""
    suffix = path.suffix.lower()
    if suffix in {".txt", ".md", ".csv", ".tsv"}:
        raw = path.read_bytes()[: max_chars * 4]
        try:
            return raw.decode("utf-8-sig")[:max_chars]
        except UnicodeDecodeError:
            return raw.decode("cp1252", errors="replace")[:max_chars]
    if suffix == ".docx":
        import zipfile
        import xml.etree.ElementTree as ET
        ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
        with zipfile.ZipFile(path) as zf:
            info = zf.getinfo("word/document.xml")
            if info.file_size > 50 * 1024 * 1024:
                raise ValueError("Documento grande demais.")
            root = ET.fromstring(zf.read(info))
        paragraphs = ["".join(t.text or "" for t in p.iter(f"{ns}t")) for p in root.iter(f"{ns}p")]
        return "\n\n".join(p for p in paragraphs if p.strip())[:max_chars]
    if suffix == ".pdf":
        try:
            from pypdf import PdfReader  # type: ignore
        except ImportError as err:
            raise RuntimeError("Para ler PDF instale o pacote pypdf (pip install pypdf).") from err
        reader = PdfReader(str(path))
        parts: List[str] = []
        total = 0
        for page in reader.pages[:200]:
            text = page.extract_text() or ""
            parts.append(text)
            total += len(text)
            if total >= max_chars:
                break
        return "\n\n".join(parts)[:max_chars]
    raise ValueError("Formato de documento não suportado (use .docx, .pdf, .txt ou .md).")

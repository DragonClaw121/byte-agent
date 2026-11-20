"""v13.26 (Fase 2.1): planilhas e documentos novos, também a partir de modelos do usuário."""
import json

import docx
import openpyxl
import pytest

from byte_agent import documentos, engine, headless
from test_native_tools import TOOL_MODEL, FakeOllama, call


def test_safe_formula_translates_portuguese_and_blocks_dangerous():
    assert documentos.safe_formula("=SOMA(B2:B9)") == "=SUM(B2:B9)"
    assert documentos.safe_formula('=SE(D{linha}>100;"Alto";"Baixo")', 5) == '=IF(D5>100,"Alto","Baixo")'
    assert documentos.safe_formula('=SE(A1="SOMA(x); SE(";1;0)') == '=IF(A1="SOMA(x); SE(",1,0)'   # texto entre aspas intacto
    assert documentos.safe_formula("B{linha}*C{linha}", 3) == "=B3*C3"
    for bad in ('=HYPERLINK("http://mal.example","x")', "=WEBSERVICE(A1)", "=[outro.xlsx]Plan1!A1",
                "=cmd|' /c calc'!A0", '=HIPERLINK("x")', "=INDIRECT(A1)"):
        with pytest.raises(documentos.FormulaError):
            documentos.safe_formula(bad)


def test_spreadsheet_with_types_formulas_total_and_chart(tmp_path):
    path = tmp_path / "orc.xlsx"
    rep = documentos.write_spreadsheet(path, {
        "titulo": "Orçamento de novembro",
        "colunas": [{"nome": "Item"}, {"nome": "Qtd", "tipo": "numero"}, {"nome": "Preço", "tipo": "moeda"},
                    {"nome": "Subtotal", "tipo": "moeda", "formula": "=B{linha}*C{linha}"},
                    {"nome": "Vencimento", "tipo": "data"}, {"nome": "Desconto", "tipo": "porcentagem"}],
        "linhas": [["Aluguel", 1, "1.500,00", "", "05/12/2026", "10%"], ["Mercado", "4", 250.5, None, "2026-12-10", 0.05]],
        "grafico": {"tipo": "pizza", "categorias": "Item", "valores": ["Subtotal"]},
        "resumo": [["Maior gasto", "=MÁXIMO(D4:D5)"]]})
    assert rep["formulas"] == 2 + 3 + 1 and rep["graficos"] == 1
    ws = openpyxl.load_workbook(path).active
    assert ws["A1"].value == "Orçamento de novembro" and ws["A3"].value == "Item"
    assert ws["C4"].value == 1500.0 and ws["C4"].number_format.startswith('"R$"') and ws["B5"].value == 4.0
    assert ws["D4"].value == "=B4*C4" and ws["D5"].value == "=B5*C5"
    assert ws["E4"].value.year == 2026 and ws["E4"].number_format == "DD/MM/YYYY"
    assert ws["F4"].value == pytest.approx(0.10) and ws["F5"].value == pytest.approx(0.05)
    assert ws["A6"].value == "Total" and ws["C6"].value == "=SUM(C4:C5)" and ws["D6"].value == "=SUM(D4:D5)"
    assert ws["A8"].value == "Maior gasto" and ws["B8"].value == "=MAX(D4:D5)"
    assert ws.freeze_panes == "A4" and ws.auto_filter.ref == "A3:F5" and len(ws._charts) == 1


def test_document_from_scratch_and_from_template(tmp_path):
    out = tmp_path / "rel.docx"
    rep = documentos.write_document(out, {"titulo": "Relatório mensal", "subtitulo": "Novembro", "secoes": [
        {"titulo": "Resumo", "paragrafos": ["Vendas cresceram."], "itens": ["Meta batida", "Novo cliente"]},
        {"titulo": "Números", "tabela": {"colunas": ["Mês", "Vendas"], "linhas": [["Out", "10"], ["Nov", "12"]]}}]})
    d = docx.Document(str(out))
    texts = [p.text for p in d.paragraphs]
    assert "Relatório mensal" in texts and "Meta batida" in texts and rep["faltando"] == []
    assert d.tables[0].rows[2].cells[1].text == "12" and d.core_properties.title == "Relatório mensal"

    tpl = tmp_path / "modelo_proposta.docx"
    t = docx.Document()
    t.sections[0].header.paragraphs[0].text = "ACME Consultoria — {{cliente}}"
    t.add_paragraph("Proposta para {{cliente}}")
    p = t.add_paragraph("Valor: ")
    p.add_run("{{valor}}").bold = True
    split = t.add_paragraph("Responsável: {{res")
    split.add_run("ponsavel}}")
    t.add_paragraph("{{conteudo}}")
    t.add_paragraph("Validade: {{validade}}")
    t.add_paragraph("Atenciosamente.")
    t.save(str(tpl))
    assert documentos.template_fields(tpl) == ["cliente", "valor", "responsavel", "conteudo", "validade"]
    out2 = tmp_path / "prop.docx"
    rep = documentos.write_document(out2, {"titulo": "Proposta", "campos": {"Cliente": "Padaria Sol", "valor": "R$ 4.000", "responsavel": "Ana"},
                                           "secoes": [{"titulo": "Escopo", "itens": ["Diagnóstico", "Plano de ação"]}]}, tpl)
    d = docx.Document(str(out2))
    texts = [p.text for p in d.paragraphs]
    assert texts[0] == "Proposta para Padaria Sol" and texts[1] == "Valor: R$ 4.000"
    assert [r.bold for r in d.paragraphs[1].runs] == [None, True]                 # o negrito do modelo continua
    assert texts[2] == "Responsável: Ana"
    assert texts[3:6] == ["Escopo", "Diagnóstico", "Plano de ação"]               # no lugar do {{conteudo}}
    assert texts[6] == "Validade: [PREENCHER: validade]" and rep["faltando"] == ["validade"]
    assert d.sections[0].header.paragraphs[0].text == "ACME Consultoria — Padaria Sol"


def test_xlsx_template(tmp_path):
    tpl = tmp_path / "modelo_recibo.xlsx"
    book = openpyxl.Workbook()
    book.active["A1"], book.active["B2"], book.active["B3"] = "Recibo de {{cliente}}", "{{valor}}", "{{data}}"
    book.save(str(tpl))
    rep = documentos.fill_xlsx_template(tmp_path / "r.xlsx", tpl, {"cliente": "João", "valor": "1.250,00"})
    ws = openpyxl.load_workbook(tmp_path / "r.xlsx").active
    assert ws["A1"].value == "Recibo de João" and ws["B2"].value == 1250.0 and ws["B3"].value == "[PREENCHER: data]"
    assert rep["faltando"] == ["data"]


@pytest.fixture()
def bit(tmp_path):
    fake = FakeOllama()
    service = headless.build_service(tmp_path, ollama_url=fake.url, model=TOOL_MODEL, profile="trabalho")
    yield fake, service, tmp_path
    service.shutdown()
    fake.httpd.shutdown()
    engine.apply_profile("pessoal")


def test_bit_creates_spreadsheet_from_chat_and_lists_templates(bit):
    fake, service, folder = bit
    fake.script = [call("create_spreadsheet", titulo="Controle de gastos",
                        colunas=[{"nome": "Gasto"}, {"nome": "Valor", "tipo": "moeda"}],
                        linhas=[["Luz", "180"], ["Internet", "120"]], grafico={"tipo": "barras"}),
                   {"content": "Pronto: sua planilha de gastos tem total e gráfico."}]
    res = service.dispatch_request("chat", {"message": "Monte uma planilha de controle de gastos com luz 180 e internet 120"})
    text = res["response"]
    assert "Arquivos gerados em entregas/: planilha_controle-de-gastos_" in text
    name = text.split("Arquivos gerados em entregas/: ")[1].split()[0]
    ws = openpyxl.load_workbook(folder / "entregas" / name).active
    assert ws["A6"].value == "Total" and ws["B6"].value == "=SUM(B4:B5)"
    assert service.file_provider(name) is not None                     # o Telegram consegue mandar o arquivo

    t = docx.Document()
    t.add_paragraph("Ata da reunião com {{cliente}} em {{data}}")
    t.save(str(folder / "arquivos" / "modelo_ata.docx"))
    out = json.loads(service.run_agent_tool("list_templates", {}, {"blocks": [], "executions": [], "wrote": False}))
    assert out["modelos"] == [{"name": "modelo_ata.docx", "campos": ["cliente", "data"]}]
    res = service.extra_actions["work_document"]({"titulo": "Ata", "modelo": "modelo_ata", "campos": {"cliente": "ACME", "data": "20/11"}})
    assert res["faltando"] == [] and (folder / "entregas" / res["arquivo"]).exists()
    bad = service.run_agent_tool("create_spreadsheet", {"titulo": "x", "colunas": ["A"], "linhas": [['=WEBSERVICE("http://x")']]},
                                 {"blocks": [], "executions": [], "wrote": False})
    assert bad.startswith("ERRO: ") and "não permitid" in bad

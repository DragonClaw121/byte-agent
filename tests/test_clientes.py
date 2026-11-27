"""v13.50: pesquisa de cliente — cadastro pelo CNPJ, web só sobre o cliente, dados internos e plano de abordagem."""
import json

import pytest

from byte_agent import clientes, web

CNPJ = "11222333000181"          # CNPJ válido (dígitos conferem)

REGISTRY = {"cnpj": CNPJ, "razao_social": "ASBEC SOCIEDADE BAIANA DE EDUCACAO E CULTURA LTDA", "nome_fantasia": "FACULDADE ASBEC",
            "descricao_situacao_cadastral": "ATIVA", "data_inicio_atividade": "2001-05-10",
            "cnae_fiscal_descricao": "Educação superior - graduação", "porte": "DEMAIS", "capital_social": 1500000,
            "municipio": "SALVADOR", "uf": "BA", "cnaes_secundarios": [{"codigo": 1, "descricao": "Educação profissional"}],
            "qsa": [{"nome_socio": "MARIA DA SILVA", "qualificacao_socio": "Sócio-Administrador", "data_entrada_sociedade": "2001-05-10"}]}


class FakeFetcher:
    def __init__(self):
        self.urls = []

    def get_json(self, url):
        self.urls.append(url)
        if CNPJ in url and "brasilapi" in url:
            return REGISTRY
        raise web.WebError("HTTP 404")


class FakeWeb:
    def __init__(self):
        self.fetcher = FakeFetcher()
        self.queries = []

    def search(self, query, limit=6, news=False):
        self.queries.append(query)
        if "CNPJ" in query:
            return {"results": [{"title": "ASBEC SOCIEDADE BAIANA DE EDUCACAO - CNPJ 11.222.333/0001-81", "url": "https://cnpj.x/a",
                                 "snippet": "Salvador/BA"},
                                {"title": "OUTRA EMPRESA LTDA - CNPJ 52.981.971/0001-63", "url": "https://cnpj.x/b", "snippet": ""}]}
        return {"results": [
            {"title": "Faculdade ASBEC inaugura novo campus em Salvador", "url": "https://noticias.x/asbec-campus",
             "snippet": "A Faculdade ASBEC anunciou investimento de R$ 20 milhões."},
            {"title": "50 perguntas de pesquisa de satisfação", "url": "https://surveymonkey.x/perguntas", "snippet": "dicas"},
            {"title": "Uma análise profunda da Cardano", "url": "https://coin.x/cardano", "snippet": "cripto"}]}

    def read(self, url, question="", max_chars=2200):
        return {"text": "A Faculdade ASBEC, em Salvador, abriu 2.000 vagas e contratou novo diretor financeiro, João Souza."}


class FakeDb:
    def __init__(self):
        self.audit = []

    def record_audit(self, *a):
        self.audit.append(a)


class FakeService:
    def __init__(self):
        self.web = FakeWeb()
        self.db = FakeDb()
        self.docs = []
        self.extra_actions = {"work_document": lambda spec: (self.docs.append(spec), {"arquivo": "documento_plano.docx"})[1]}


def fake_llm(calls):
    def llm(system, user, image_url="", json_format=True, max_tokens=1500):
        calls.append({"system": system, "user": user, "image": image_url})
        if "Identifique o CLIENTE" in system:
            return json.dumps({"nome": "ASBEC-SOCIEDADE BAIANA DE EDUCACAO E CULTURA LTDA", "cnpj": "", "cidade": "Salvador",
                               "dados_internos": "Comprou 3 lotes de notebooks em 2025 (R$ 180 mil); último pedido em 06/2025.",
                               "foco": "reativar"})
        return json.dumps({"titulo": "Plano de conta — Faculdade ASBEC", "resumo": "Cliente forte, sem pedidos desde 06/2025 [interno]; "
                           "acaba de abrir campus novo [1]: hora de reativar.",
                           "secoes": [{"titulo": "Estratégia de acionamento", "itens": ["Procurar João Souza, diretor financeiro [1]"]}],
                           "email": {"assunto": "Equipamentos para o novo campus", "corpo": "Olá, João! ...\n\n[SEU NOME]"}})
    return llm


def test_cnpj_helpers():
    assert clientes.cnpj_valid(CNPJ) and not clientes.cnpj_valid("11222333000182") and not clientes.cnpj_valid("11111111111111")
    assert clientes.fmt_cnpj(CNPJ) == "11.222.333/0001-81"
    assert clientes.find_cnpjs("a 11.222.333/0001-81 e 11.222.333/0001-82") == [CNPJ]
    assert clientes.key_terms("ASBEC - Sociedade Baiana de Educação Ltda") == ["asbec", "baiana", "educacao"]
    terms = clientes.key_terms("FACULDADE ASBEC")
    assert clientes.relevant("Faculdade ASBEC inaugura campus", terms, ["faculdade asbec"])
    assert not clientes.relevant("Uma análise profunda da Cardano", terms, ["faculdade asbec"])


def test_research_finds_cnpj_registry_filters_web_and_builds_the_plan():
    svc, calls = FakeService(), []
    research = clientes.ClientResearch(svc, llm=fake_llm(calls))
    res = research.run("Me auxilie com uma pesquisa profunda sobre esse cliente", notes="Tela do ERP: ASBEC, pedidos 2025",
                       image_url="data:image/jpeg;base64,xx")
    assert res["cnpj"] == "11.222.333/0001-81" and res["cadastro"] and res["cliente"] == "FACULDADE ASBEC"
    assert calls[0]["image"].startswith("data:image") and "Tela do ERP" in calls[0]["user"]            # identificou pela tela
    assert any(q == '"ASBEC-SOCIEDADE BAIANA DE EDUCACAO E CULTURA LTDA" CNPJ' for q in svc.web.queries)
    assert any('"FACULDADE ASBEC"' in q for q in svc.web.queries)                                      # buscas com o nome exato
    assert [s["url"] for s in res["fontes"]] == ["https://noticias.x/asbec-campus"]                     # só fonte sobre o cliente
    plan_prompt = calls[-1]["user"]
    assert "R$ 180 mil" in plan_prompt and "MARIA DA SILVA" in plan_prompt and "2.000 vagas" in plan_prompt
    assert "nunca invente" in calls[-1]["system"]
    doc = svc.docs[0]
    titles = [s["titulo"] for s in doc["secoes"]]
    assert titles[0] == "Cadastro oficial (Receita Federal)" and "Sócios e administradores (cadastro)" in titles
    assert "Rascunho do e-mail de abordagem" in titles and titles[-1] == "Fontes"
    assert ["Capital social", "R$ 1.500.000,00"] in doc["secoes"][0]["tabela"]["linhas"]
    assert res["arquivos"] == ["documento_plano.docx"] and "Arquivos gerados em entregas/: documento_plano.docx" in res["texto"]
    assert res["email"]["assunto"] == "Equipamentos para o novo campus"


def test_research_without_name_asks_for_it():
    svc = FakeService()
    research = clientes.ClientResearch(svc, llm=lambda *a, **k: json.dumps({"nome": "", "cnpj": ""}))
    with pytest.raises(ValueError, match="identificar o cliente"):
        research.run("pesquise esse cliente")


def test_vague_request_is_not_searched_as_a_phrase_and_client_asks_are_detected():
    assert web.search_query_from("Me auxilie com uma pesquisa profunda sobre esse cliente") == ""
    assert web.search_query_from("Consegue fazer uma pesquisa sobre a empresa: ENGINEERING DO BRASIL S/A?") == \
        "empresa: ENGINEERING DO BRASIL S/A"
    assert clientes.CLIENT_RE.search(clientes.fold("Me auxilie com uma pesquisa profunda sobre esse cliente"))
    assert clientes.CLIENT_RE.search(clientes.fold("faça um levantamento da empresa ACME"))
    assert not clientes.CLIENT_RE.search(clientes.fold("pesquise o preço do dólar"))
    assert web._proper_terms('pesquisa sobre o cliente ASBEC e "Faculdade Baiana"') == ["faculdade baiana", "asbec"]


class LinkedInWeb(FakeWeb):
    def search(self, query, limit=6, news=False, site=""):
        self.queries.append((site, query))
        if site == "linkedin.com/company":
            return {"results": [{"title": "Faculdade ASBEC | LinkedIn", "url": "https://br.linkedin.com/company/faculdade-asbec?trk=x",
                                 "snippet": "Faculdade ASBEC | 1.200 seguidores no LinkedIn. Educação superior em Salvador."}]}
        if site == "linkedin.com/in":
            return {"results": [
                {"title": "Carla Mendes - Coordenadora de TI - Faculdade ASBEC | LinkedIn",
                 "url": "https://br.linkedin.com/in/carla-mendes", "snippet": "Coordenadora de TI na Faculdade ASBEC."},
                {"title": "Pedro Lima - Analista - Outra Empresa | LinkedIn", "url": "https://br.linkedin.com/in/pedro",
                 "snippet": "Analista na Outra Empresa."},
                {"title": "Página qualquer", "url": "https://site.x/carla", "snippet": "Faculdade ASBEC"}]}
        return super().search(query, limit, news)


def test_research_brings_linkedin_company_and_people(monkeypatch):
    """v13.75: a página da empresa e os perfis públicos (pelo buscador; o LinkedIn não é aberto) e as pessoas vistas na tela."""
    svc, calls = FakeService(), []
    svc.web = LinkedInWeb()
    base = fake_llm(calls)

    def llm(system, user, image_url="", json_format=True, max_tokens=1500):
        out = json.loads(base(system, user, image_url, json_format, max_tokens))
        if "Identifique o CLIENTE" in system:
            out["pessoas_na_tela"] = [{"nome": "Roberto Alves", "cargo": "Diretor Financeiro", "empresa": "Faculdade ASBEC"}]
        else:
            out["secoes"].insert(0, {"titulo": "Pessoas-chave e decisores", "itens": ["Carla Mendes [L2]"]})
            out["linkedin"] = {"empresa": "https://br.linkedin.com/company/faculdade-asbec",
                               "pessoas": [{"nome": "Carla Mendes", "cargo": "Coordenadora de TI", "por_que": "usa as licenças",
                                            "url": "https://br.linkedin.com/in/carla-mendes", "fonte": "L2"},
                                           {"nome": "Roberto Alves", "cargo": "Diretor Financeiro", "por_que": "aprova a compra",
                                            "url": "https://br.linkedin.com/in/inventado", "fonte": "tela"}],
                               "nota_conexao": "Oi, Carla! Trabalho com a gestão de licenças da ASBEC; vamos nos conectar?",
                               "como_usar": "Veja as vagas de TI abertas."}
        return json.dumps(out)
    research = clientes.ClientResearch(svc, llm=llm)
    res = research.run("pesquise a Faculdade ASBEC", image_url=["data:image/jpeg;base64,a", "data:image/jpeg;base64,b"])
    assert ("linkedin.com/in", '"FACULDADE ASBEC" diretor OR gerente OR head OR coordenador OR compras OR TI OR financeiro') \
        in svc.web.queries
    plan_prompt = calls[-1]["user"]
    assert "[L1] (empresa) Faculdade ASBEC | LinkedIn — https://br.linkedin.com/company/faculdade-asbec" in plan_prompt
    assert "Carla Mendes" in plan_prompt and "Pedro Lima" not in plan_prompt and "site.x" not in plan_prompt
    assert "Roberto Alves — Diretor Financeiro" in plan_prompt                          # visto na tela (LinkedIn aberto)
    li = res["linkedin"]
    assert li["empresa"] == "https://br.linkedin.com/company/faculdade-asbec"
    assert [p["url"] for p in li["pessoas"]] == ["https://br.linkedin.com/in/carla-mendes", ""]   # link inventado sai
    titles = [s["titulo"] for s in svc.docs[0]["secoes"]]
    assert titles.index("LinkedIn: empresa e pessoas") == titles.index("Pessoas-chave e decisores") + 1
    sec = next(s for s in svc.docs[0]["secoes"] if s["titulo"] == "LinkedIn: empresa e pessoas")
    assert sec["tabela"]["linhas"][1][3] == "vista na tela"
    assert "🔗 LinkedIn: https://br.linkedin.com/company/faculdade-asbec" in res["texto"]
    assert "Convite sugerido: Oi, Carla!" in res["texto"]
    assert any("[L2] LinkedIn" in i for i in svc.docs[0]["secoes"][-1]["itens"])

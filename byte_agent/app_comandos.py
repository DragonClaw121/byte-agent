"""O app do celular como canal principal: conversa escrita e os MESMOS comandos do Telegram — v13.30 (app 2.0).

O app manda POST /api/v1/command {"text": "/clima"} (ou uma mensagem comum, ou {"callback": "ok:<id>"} de um botão
Aprovar/Descartar) e recebe as mensagens que o Telegram receberia, com os botões. Por baixo é a própria ponte do
Telegram rodando "por dentro" (sem rede): todo comando novo do Telegram já funciona no app.
GET /api/v1/commands devolve o catálogo dos botões, só com o que este agente tem.
"""
from __future__ import annotations

import contextlib
import re
import threading
from typing import Any, Dict, List, Optional

from .telegram_bridge import EMAIL_CARD_RE, TelegramBridge

APP_CHAT = 1
FILES_RE = re.compile(r"Arquivos gerados em entregas/: ([^\n]+)")

# (grupo, rótulo do botão, comando, pergunta quando precisa de texto, exemplo, requisito)
CATALOG: List[tuple] = [
    ("Dia a dia", "☀️ Resumo do dia", "/resumo", "", "", ""),
    ("Dia a dia", "🌤️ Clima", "/clima", "Cidade (vazio = onde você está)", "Campinas", "opcional"),
    ("Dia a dia", "📍 Onde estou", "/onde", "", "", ""),
    ("Dia a dia", "🧭 Minha rotina", "/rotina", "", "", ""),
    ("Dia a dia", "📊 Fechamento da semana", "/semana", "", "", ""),
    ("Agenda", "📅 Compromissos de hoje", "/compromissos hoje", "", "", ""),
    ("Agenda", "📆 Compromissos da semana", "/compromissos semana", "", "", ""),
    ("Agenda", "🗓️ Tarefas", "/agenda", "", "", ""),
    ("Agenda", "➕ Nova tarefa", "/tarefa", "O que você precisa fazer?", "Ligar para o banco amanhã", "obrigatorio"),
    ("Agenda", "✅ Concluir tarefa", "/feito", "Número ou parte do título", "conta de luz", "obrigatorio"),
    ("Agenda", "⏰ Novo lembrete", "/lembrete", "Quando e o quê", "30m beber água  |  amanhã 09:00 reunião", "obrigatorio"),
    ("Agenda", "📋 Lembretes", "/lembretes", "", "", ""),
    ("Agenda", "🗑️ Cancelar lembrete", "/cancelar", "Número do lembrete", "12", "obrigatorio"),
    ("Reuniões", "📋 Reuniões gravadas", "/reunioes", "", "", ""),
    ("Reuniões", "📝 Ver uma ata", "/ata", "Número da reunião", "3", "obrigatorio"),
    ("Memória e objetivos", "🧠 Memórias", "/memorias", "", "", ""),
    ("Memória e objetivos", "💾 Guardar na memória", "/lembrar", "O que devo lembrar?", "Prefiro reuniões de manhã", "obrigatorio"),
    ("Memória e objetivos", "🧩 Objetivos", "/objetivos", "", "", ""),
    ("Memória e objetivos", "🎯 Novo objetivo", "/objetivo", "O que você quer receber no fim?", "Compare 3 planos de celular", "obrigatorio"),
    ("Memória e objetivos", "▶️ Retomar objetivo", "/retomar", "Número do objetivo", "2", "obrigatorio"),
    ("Memória e objetivos", "⛔ Cancelar objetivo", "/cancelar_objetivo", "Número do objetivo", "2", "obrigatorio"),
    ("Pesquisa", "🔎 Busca rápida", "/buscar", "O que procurar?", "preço do dólar hoje", "obrigatorio:web"),
    ("Pesquisa", "🏢 Pesquisar cliente", "/cliente", "Nome ou CNPJ (e, depois de |, o que você sabe das compras)",
     "ACME Ltda | comprou 3x em 2026, último pedido R$ 12 mil em out", "obrigatorio:web"),
    ("Pesquisa", "📚 Pesquisa profunda", "/pesquisar", "Pergunta da pesquisa", "Vale a pena carro elétrico em 2027?", "obrigatorio:web"),
    ("E-mail", "📬 Caixas", "/email", "", "", "email"),
    ("E-mail", "📥 Não lidos", "/caixa", "", "", "email"),
    ("E-mail", "🗂️ Triagem", "/triagem", "", "", "email"),
    ("E-mail", "📖 Abrir um e-mail", "/abrir", "Referência conta:número (da lista)", "gmail:3", "obrigatorio:email"),
    ("E-mail", "➕ Adicionar caixa", "/email adicionar", "Endereço (e um id opcional)", "voce@empresa.com.br trabalho", "obrigatorio:email"),
    ("E-mail", "🔑 Entrar na Microsoft (caixa)", "/email entrar", "Id da conta", "outlook", "obrigatorio:email"),
    ("E-mail", "🧪 Testar caixa", "/email testar", "Id da conta", "gmail", "obrigatorio:email"),
    ("E-mail", "🧩 Modelos de e-mail", "/modelos_email", "", "", ""),
    ("E-mail", "✍️ Assinatura", "/assinatura", "Sua assinatura (vazio = mostrar a atual)", "Gabriel Amorim | Comercial", "opcional"),
    ("Trabalho", "📁 Arquivos", "/arquivos", "", "", "trabalho"),
    ("Trabalho", "📑 Modelos", "/modelos", "", "", "trabalho"),
    ("Trabalho", "📈 Analisar arquivo", "/analise", "Arquivo (e uma pergunta, opcional)", "vendas.xlsx qual mês vendeu mais?", "obrigatorio:trabalho"),
    ("Trabalho", "📄 Resumir documento", "/ler", "Arquivo (e uma pergunta, opcional)", "contrato.pdf quais os prazos?", "obrigatorio"),
    ("Trabalho", "🖼️ Apresentação", "/apresentacao", "Tema da apresentação", "Resultados do trimestre", "obrigatorio:trabalho"),
    ("Contas", "🟦 Microsoft (agenda, OneDrive, Teams)", "/microsoft", "", "", ""),
    ("Contas", "🔑 Conectar a Microsoft", "/microsoft entrar", "", "", ""),
    ("Contas", "🟩 Google (agenda e Drive)", "/google", "", "", ""),
    ("Contas", "🔑 Conectar o Google", "/google entrar", "", "", ""),
    ("Pessoas", "👥 Pessoas", "/pessoas", "", "", ""),
    ("Pessoas", "➕ Convidar pessoa", "/convidar", "Nome (e \"admin\" no fim para administrador)", "Ana", "obrigatorio"),
    ("Pessoas", "🖥️ Painel de uma pessoa", "/pessoas painel", "Número da pessoa", "1", "obrigatorio"),
    ("Pessoas", "🚫 Remover pessoa", "/remover", "Número da pessoa (e \"apagar\" para apagar os dados)", "1", "obrigatorio"),
    ("Pessoas", "✉️ Novo convite", "/pessoas convite", "Número da pessoa", "1", "obrigatorio"),
    ("Pessoas", "📊 Limite diário", "/limite", "Número da pessoa e mensagens por dia", "1 300", "obrigatorio"),
    ("Aparelhos", "💻 Conectar o PC", "/pc", "", "", "pc"),
    ("Aparelhos", "📱 Conectar celular/relógio", "/parear", "", "", "api"),
    ("Aparelhos", "📋 Aparelhos conectados", "/dispositivos", "", "", ""),
    ("Aparelhos", "🔌 Desconectar aparelho", "/desparear", "Número do aparelho (veja a lista)", "3", "obrigatorio"),
    ("Agente", "🔐 Permissões", "/permissoes", "", "", ""),
    ("Agente", "🤖 Atividade", "/atividade", "", "", ""),
    ("Agente", "⚡ Gatilhos", "/gatilhos", "", "", ""),
    ("Agente", "🔁 Ligar/desligar gatilho", "/gatilho", "Nome e on/off", "email off", "obrigatorio"),
    ("Agente", "🔐 Mudar uma permissão", "/permissao", "Tipo e nível (sugerir, aprovar, automatico)", "tarefas automatico", "obrigatorio"),
    ("Agente", "💬 Iniciativas (puxar assunto)", "/iniciativas", "on ou off", "off", "obrigatorio"),
    ("Agente", "🧠 Modelo em uso", "/modelo", "", "", ""),
    ("Agente", "🤖 Agente ativo", "/agente", "", "", ""),
    ("Agente", "☀️ Ajustar resumos", "/resumo config", "", "", ""),
    ("Agente", "📈 Avaliar comportamento", "/avaliar", "", "", ""),
    ("Agente", "🔄 Atualizar", "/atualizar", "", "", ""),
    ("Agente", "🖥️ Links dos painéis", "/painel", "", "", ""),
    ("Agente", "🩺 Saúde", "/saude", "", "", ""),
    ("Agente", "👨‍👩‍👧 Saúde de todos", "/saude todos", "", "", ""),
    ("Agente", "📶 Status", "/status", "", "", ""),
    ("Agente", "🧹 Limpar conversa", "/limpar", "", "", ""),
    ("Agente", "❓ Todos os comandos", "/ajuda", "", "", ""),
]


class AppBridge(TelegramBridge):
    """A ponte do Telegram sem o Telegram: as mensagens vão para uma lista que volta na resposta HTTP."""

    def __init__(self, service: Any):                     # noqa: D401 — não chama o __init__ do pai (sem token)
        self._eval_lock = threading.Lock()
        self.service = service
        self._token = ""
        self.allowed = {APP_CHAT}
        self.api_base = ""
        self.poll_timeout_s = 0
        self._offset = 0
        self._stop = threading.Event()
        self._thread = None
        self._pending_cards: Dict[str, Dict[str, str]] = {}
        self.stream_director = None
        self.last_error: Optional[str] = None
        self._lock = threading.Lock()
        self._out: List[Dict[str, Any]] = []

    # ---------------- "Telegram" local
    def _api(self, method: str, payload: Optional[Dict[str, Any]] = None, timeout_s: float = 20.0) -> Dict[str, Any]:
        return {"ok": True, "result": {}}

    def send(self, chat_id: int, text: str, reply_markup: Optional[Dict[str, Any]] = None) -> None:
        buttons = []
        for row in (reply_markup or {}).get("inline_keyboard") or []:
            for b in row:
                if b.get("callback_data"):
                    buttons.append({"label": str(b.get("text") or ""), "data": str(b["callback_data"])})
        self._out.append({"text": str(text or ""), "buttons": buttons})

    def send_document(self, chat_id: int, path: Any, caption: str = "") -> None:
        name = str(getattr(path, "name", None) or str(path).rsplit("/", 1)[-1])
        self._out.append({"text": f"📎 {name} está pronto (o Telegram e o painel entregam o arquivo).", "buttons": []})

    def _send_file(self, *args: Any, **kwargs: Any) -> None:
        pass

    def _typing(self, chat_id: int) -> Any:
        return contextlib.nullcontext()

    def run(self, text: str = "", callback: str = "") -> List[Dict[str, Any]]:
        with self._lock:
            self._out = []
            try:
                if callback:
                    self._handle_callback(APP_CHAT, {"id": "app", "data": callback})
                elif text.startswith("/"):
                    self._handle_command(APP_CHAT, text)
                elif text:
                    self._handle_chat(APP_CHAT, text)
            except Exception as err:                       # um comando com problema não derruba o app
                reason = re.sub(r"^\w*Error: ", "", str(err))[:300]
                self._out.append({"text": f"⚠️ {reason}", "buttons": []})
            out, self._out = self._out, []
        for msg in out:                                    # cartões de aprovação já viraram botões: tira o JSON do texto
            msg["text"] = EMAIL_CARD_RE.sub("", msg["text"]).strip()
        return [m for m in out if m["text"] or m["buttons"]]


def catalog(service: Any) -> List[Dict[str, Any]]:
    tools = getattr(service, "extra_tools", {})
    actions = getattr(service, "extra_actions", {})
    disabled = getattr(service, "disabled_tools", set())
    have = {
        "web": "web_search" in tools and "web_search" not in disabled,
        "email": "email_listar" in tools and "email_listar" not in disabled,
        "trabalho": "work_status" in actions,
        "pc": "pc_pair_code" in actions,                                   # v13.48
        "api": bool(getattr(service, "api_enabled", False)),               # /parear: só onde há app (Kronus)
    }
    out = []
    for group, label, cmd, ask, example, need in CATALOG:
        req = need.split(":", 1)[-1] if need else ""
        if req in have and not have[req]:
            continue
        out.append({"group": group, "label": label, "command": cmd, "ask": ask, "example": example,
                    "required": need.startswith("obrigatorio")})
    return out


def install(service: Any) -> AppBridge:
    bridge = AppBridge(service)
    service.app_bridge = bridge
    service.extra_actions.update({
        "app_command": lambda p: {"messages": bridge.run(str(p.get("text") or "").strip()[:2000], str(p.get("callback") or ""))},
        "app_commands": lambda p: {"commands": catalog(service)},
    })
    return bridge

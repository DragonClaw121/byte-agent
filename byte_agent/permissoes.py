"""Níveis de permissão por tipo de ação — Fase 1.3 (v13.23).

Cada tipo de ação que MUDA algo tem um nível escolhido pelo usuário:
  sugerir     a Byte só sugere ("quer que eu crie a tarefa X?"); nada é executado
  aprovar     prepara um cartão "Aprovar / Descartar"; só executa o que foi mostrado
  automatico  faz sozinha e registra em /atividade

Ações que saem para fora (e-mail, integrações) e código têm PISO "aprovar": não podem ser automáticas nesta versão.
Leituras (agenda, memórias, busca na web) não passam por aqui.
Em tarefas desassistidas (objetivos, rotinas), "aprovar" manda o cartão para o Telegram/painel e a etapa segue.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, List, Optional

LEVELS = ("sugerir", "aprovar", "automatico")
LEVEL_LABEL = {"sugerir": "só sugerir", "aprovar": "pedir aprovação", "automatico": "fazer e registrar"}

CATEGORIES: Dict[str, Dict[str, Any]] = {
    "lembretes": {"label": "Lembretes e rotinas", "tools": {"schedule_reminder", "cancel_reminder"}, "default": "automatico"},
    "tarefas": {"label": "Tarefas da agenda", "tools": {"add_task"}, "default": "automatico"},
    "memorias": {"label": "Memórias (fatos e preferências)", "tools": {"add_memory"}, "default": "automatico"},
    "objetivos": {"label": "Objetivos de longo prazo", "tools": {"criar_objetivo"}, "default": "automatico"},
    "codigo": {"label": "Rodar código", "tools": {"run_python", "run_shell"}, "default": "aprovar", "floor": "aprovar"},
    "email": {"label": "Enviar e-mail", "tools": {"email_send"}, "default": "aprovar", "floor": "aprovar"},
    "integracoes": {"label": "Escrever no Asana, Notion e Teams",
                    "tools": {"asana_create_task", "notion_create_page", "teams_post"}, "default": "aprovar", "floor": "aprovar"},
}
# Estas têm cartão próprio (fluxo antigo); as demais usam o cartão genérico "ferramenta".
OWN_APPROVAL = {"codigo", "email", "integracoes"}
SETTING = "permissoes"
ACTIVITY_EVENTS = ("acao_automatica", "tool_approved", "tool_suggested", "tool_proposed", "goal_created", "goal_done",
                   "tool_rejected", "tool_blocked")

_TOOL_TO_CATEGORY = {tool: cat for cat, spec in CATEGORIES.items() for tool in spec["tools"]}


def category_of(tool: str) -> Optional[str]:
    return _TOOL_TO_CATEGORY.get(tool)


def levels(db: Any) -> Dict[str, str]:
    saved = db.get_setting(SETTING, {}) or {}
    out = {}
    for cat, spec in CATEGORIES.items():
        lvl = saved.get(cat, spec["default"])
        out[cat] = _clamp(cat, lvl if lvl in LEVELS else spec["default"])
    return out


def _clamp(cat: str, level: str) -> str:
    floor = CATEGORIES[cat].get("floor")
    if floor == "aprovar" and level == "automatico":
        return "aprovar"
    return level


def set_level(db: Any, cat: str, level: str) -> Dict[str, str]:
    cat = (cat or "").strip().lower()
    level = (level or "").strip().lower().replace("automático", "automatico")
    if cat not in CATEGORIES:
        raise ValueError(f"Tipo de ação desconhecido. Use um de: {', '.join(CATEGORIES)}.")
    if level not in LEVELS:
        raise ValueError("Nível deve ser sugerir, aprovar ou automatico.")
    if _clamp(cat, level) != level:
        raise PermissionError(f"'{CATEGORIES[cat]['label']}' sai para fora do seu controle (ou roda código): "
                              "nesta versão o máximo é 'aprovar'.")
    saved = dict(db.get_setting(SETTING, {}) or {})
    saved[cat] = level
    db.set_setting(SETTING, saved)
    db.record_audit("permission_changed", "permissoes", "success", {"categoria": cat, "nivel": level})
    return levels(db)


def describe_call(tool: str, args: Dict[str, Any]) -> str:
    """Frase curta e humana do que a ferramenta faria (cartões, sugestões e /atividade)."""
    a = args or {}
    if tool == "add_task":
        slot = f" ({a.get('scheduled_slot')})" if a.get("scheduled_slot") else ""
        return f"Criar a tarefa \"{str(a.get('title', ''))[:120]}\"{slot}"
    if tool == "add_memory":
        return f"Guardar na memória: \"{str(a.get('content', ''))[:140]}\""
    if tool == "schedule_reminder":
        when = a.get("when") or (f"daqui a {a.get('in_minutes')} min" if a.get("in_minutes") else "")
        kind = "Agendar a tarefa" if a.get("run_as_task") else "Agendar o lembrete"
        rep = {"daily": " (todo dia)", "weekly": " (toda semana)"}.get(str(a.get("repeat", "")), "")
        return f"{kind} \"{str(a.get('text', ''))[:120]}\" {when}{rep}".strip()
    if tool == "cancel_reminder":
        return f"Cancelar o lembrete #{a.get('id')}"
    if tool == "criar_objetivo":
        return f"Criar o objetivo \"{str(a.get('titulo') or a.get('title') or '')[:120]}\""
    return f"Usar {tool}"


def format_levels(lv: Dict[str, str]) -> str:
    lines = ["Permissões da Byte (o que ela faz sem você):"]
    for cat, spec in CATEGORIES.items():
        extra = " (máximo: aprovar)" if spec.get("floor") else ""
        lines.append(f"• {cat} — {spec['label']}: {LEVEL_LABEL[lv[cat]]}{extra}")
    lines.append("Mudar: /permissao <tipo> sugerir|aprovar|automatico   ·   Ver o que ela fez: /atividade")
    return "\n".join(lines)


def activity(db: Any, limit: int = 15) -> List[Dict[str, Any]]:
    marks = ",".join("?" for _ in ACTIVITY_EVENTS)
    with db.connection() as conn:
        rows = conn.execute(f"SELECT event_type, operation, details_json, created_at FROM audit_log WHERE event_type IN ({marks}) "
                            "ORDER BY id DESC LIMIT ?", (*ACTIVITY_EVENTS, int(limit))).fetchall()
    out = []
    for r in rows:
        try:
            details = json.loads(r["details_json"] or "{}")
        except ValueError:
            details = {}
        out.append({"event": r["event_type"], "operation": r["operation"], "details": details, "at": r["created_at"]})
    return out


def format_activity(items: List[Dict[str, Any]]) -> str:
    if not items:
        return "Nenhuma ação registrada ainda."
    icon = {"acao_automatica": "🤖 fez", "tool_approved": "✅ aprovado", "tool_suggested": "💡 sugeriu",
            "tool_proposed": "🔐 pediu aprovação", "goal_created": "🧩 objetivo criado", "goal_done": "🏁 objetivo concluído",
            "tool_rejected": "⛔ recusado", "tool_blocked": "🛡️ bloqueado"}
    lines = ["O que a Byte fez ou propôs (mais recente primeiro):"]
    for it in items:
        d = it["details"]
        what = d.get("resumo") or d.get("title") or d.get("tool") or it["operation"]
        try:
            when = datetime.fromisoformat(str(it["at"])).astimezone().strftime("%d/%m %H:%M")
        except ValueError:
            when = str(it["at"])[5:16].replace("T", " ")
        lines.append(f"{when} · {icon.get(it['event'], it['event'])}: {what}")
    return "\n".join(lines)


def install(service: Any) -> None:
    db = service.db

    def act_set(payload: Dict[str, Any]) -> Dict[str, Any]:
        lv = set_level(db, str(payload.get("category") or ""), str(payload.get("level") or ""))
        return {"levels": lv, "text": format_levels(lv)}

    service.extra_actions.update({
        "permission_list": lambda payload: {"levels": levels(db), "text": format_levels(levels(db)),
                                            "categories": {k: {"label": v["label"], "floor": v.get("floor")} for k, v in CATEGORIES.items()}},
        "permission_set": act_set,
        "activity_list": lambda payload: dict(items=activity(db, int(payload.get("limit") or 15)),
                                              text=format_activity(activity(db, int(payload.get("limit") or 15)))),
    })
    service.status_providers["permissoes"] = lambda: {"levels": levels(db),
                                                      "categories": {k: {"label": v["label"], "floor": v.get("floor")}
                                                                     for k, v in CATEGORIES.items()}}

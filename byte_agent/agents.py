"""Núcleo replicável: cada agente (Byte, Kronus, Bit...) é um MANIFESTO JSON sobre o mesmo código.

O manifesto define identidade, personalidade, dono, habilidades ligadas e canais. Ele NUNCA guarda segredos:
só os NOMES dos segredos (Colab Secrets ou variáveis de ambiente) onde estão tokens e senhas.

    python -m byte_agent novo-agente kronus --tipo assistente      # cria agents/kronus.json
    python -m byte_agent validar-agente agents/kronus.json
    python -m byte_agent --agent agents/kronus.json                  # sobe o Kronus (dados em ~/.byte-agents/kronus)

Limite atual: um agente por processo (o motor guarda o perfil ativo em variáveis do módulo). Vários agentes = vários
processos, cada um com seu manifesto, sua pasta e sua porta — veja deploy/docker-compose.agentes.yml.
"""
from __future__ import annotations

import json
import os
import re
import secrets as pysecrets
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

AGENT_TYPES = {
    "assistente": "Assistente pessoal (agenda, lembretes, memória, conversa)",
    "trabalho": "Assistente de trabalho (e-mails, planilhas, apresentações, pesquisa)",
    "vtuber": "Personagem de entretenimento ao vivo (Twitch, VTube Studio, voz)",
}

# Habilidade -> ferramentas que ela libera. get_datetime fica sempre ligada.
SKILLS: Dict[str, Tuple[str, ...]] = {
    "agenda": ("list_tasks", "add_task", "complete_task"),
    "memoria": ("search_memories", "add_memory"),
    "lembretes": ("schedule_reminder", "list_reminders", "cancel_reminder"),
    "web": ("web_search", "fetch_url", "deep_research"),
    "email": ("email_inbox", "email_send", "email_triage"),
    "dados": ("analyze_data", "list_work_files", "create_spreadsheet"),
    "apresentacoes": ("create_presentation",),
    "codigo": ("run_python", "run_shell"),
    "integracoes": ("asana_my_tasks", "asana_create_task", "notion_search", "notion_read_page", "notion_create_page", "teams_post"),
    "documentos": ("read_document", "create_document", "list_templates"),
}
ALWAYS_ON_TOOLS = ("get_datetime",)
SKILL_ACTIONS: Dict[str, Tuple[str, ...]] = {          # ações do painel/Telegram que pertencem a cada habilidade
    "web": ("web_search", "web_research", "web_status"),
    "email": ("work_triage",),
    "dados": ("work_analyze", "work_upload", "work_spreadsheet"),
    "documentos": ("work_read_document", "work_document", "work_templates"),
    "apresentacoes": ("work_presentation",),
}
DEFAULT_SKILLS = {
    "assistente": ["agenda", "memoria", "lembretes", "web"],
    "trabalho": ["agenda", "memoria", "lembretes", "web", "email", "dados", "apresentacoes", "documentos", "integracoes"],
    "vtuber": ["memoria", "web"],
}

ID_RE = re.compile(r"^[a-z][a-z0-9-]{1,31}$")
SECRET_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_TOP_KEYS = {"id", "nome", "tipo", "descricao", "persona", "dono", "habilidades", "modelo", "iniciativas", "estilo", "canais", "voz"}
_CHANNEL_KEYS = {"painel", "telegram", "api", "twitch", "overlay"}


class ManifestError(ValueError):
    pass


def _check_secret_name(where: str, value: Any) -> str:
    name = str(value or "").strip()
    if name and (re.match(r"^(sk-|tvly-|xox|ghp_|gho_|oauth:|AIza)", name) or len(name) > 64):
        raise ManifestError(f"{where}: isto parece um token, não um NOME de segredo. Guarde o token nos Segredos e escreva só o nome aqui.")
    if name and not SECRET_NAME_RE.match(name):
        raise ManifestError(f"{where}: '{name}' não é um nome de segredo válido (use letras, números e _).")
    return name


def validate_manifest(raw: Any) -> Dict[str, Any]:
    """Valida e completa um manifesto. Lança ManifestError com uma mensagem clara em português."""
    if not isinstance(raw, dict):
        raise ManifestError("O manifesto deve ser um objeto JSON.")
    unknown = sorted(set(raw) - _TOP_KEYS)
    if unknown:
        raise ManifestError(f"Campos desconhecidos no manifesto: {', '.join(unknown)}.")
    m: Dict[str, Any] = json.loads(json.dumps(raw))          # cópia profunda
    agent_id = str(m.get("id") or "").strip().lower()
    if not ID_RE.match(agent_id):
        raise ManifestError("'id' deve ter 2 a 32 caracteres: letras minúsculas, números e hífen, começando por letra.")
    m["id"] = agent_id
    m["nome"] = " ".join(str(m.get("nome") or agent_id.capitalize()).split())[:40]
    kind = str(m.get("tipo") or "assistente").strip().lower()
    if kind not in AGENT_TYPES:
        raise ManifestError(f"'tipo' deve ser um de: {', '.join(AGENT_TYPES)}.")
    m["tipo"] = kind
    m["descricao"] = str(m.get("descricao") or AGENT_TYPES[kind])[:300]
    m["persona"] = str(m.get("persona") or "").strip()[:4000]
    try:                                         # v12.3: comando de voz (quem pode falar, palavra de ativação, resposta falada)
        from .voice import validate_voice
        m["voz"] = validate_voice(m.get("voz"), kind, m["nome"])
    except ValueError as err:
        raise ManifestError(str(err)) from None
    owner = m.get("dono") or {}
    if not isinstance(owner, dict):
        raise ManifestError("'dono' deve ser um objeto (usuario, cargo, empresa, assinatura...).")
    # v12.2: valores de exemplo ("Seu Nome", "Sua empresa"...) contam como não preenchidos, para o agente não se apresentar
    # como "assistente de Seu Nome".
    m["dono"] = {str(k)[:40]: str(v)[:300] for k, v in owner.items() if str(v).strip() and not _is_placeholder(v)}
    skills = m.get("habilidades")
    if skills is None:
        skills = list(DEFAULT_SKILLS[kind])
    if not isinstance(skills, list) or any(s not in SKILLS for s in skills):
        raise ManifestError(f"'habilidades' deve ser uma lista com itens entre: {', '.join(SKILLS)}.")
    if kind != "trabalho" and set(skills) & {"email", "dados", "apresentacoes", "documentos"}:
        raise ManifestError("As habilidades email, dados, apresentacoes e documentos exigem tipo 'trabalho'.")
    m["habilidades"] = list(dict.fromkeys(skills))
    m["modelo"] = str(m.get("modelo") or "").strip()[:120]

    initiatives = m.get("iniciativas") or {}
    if not isinstance(initiatives, dict):
        raise ManifestError("'iniciativas' deve ser um objeto.")
    m["iniciativas"] = {
        "ativas": bool(initiatives.get("ativas", kind == "assistente")),
        "ociosidade_min": max(1.0, float(initiatives.get("ociosidade_min", 10))),
        "intervalo_min": max(1.0, float(initiatives.get("intervalo_min", 30))),
    }
    style = m.get("estilo") or {}
    if not isinstance(style, dict):
        raise ManifestError("'estilo' deve ser um objeto.")
    theme = str(style.get("tema") or ("pro" if kind == "trabalho" else "neon")).lower()
    if theme not in {"pro", "neon"}:
        raise ManifestError("'estilo.tema' deve ser 'pro' ou 'neon'.")
    m["estilo"] = {"tema": theme, "respostas_curtas": bool(style.get("respostas_curtas", False))}

    channels = m.get("canais") or {}
    if not isinstance(channels, dict) or set(channels) - _CHANNEL_KEYS:
        raise ManifestError(f"'canais' aceita apenas: {', '.join(sorted(_CHANNEL_KEYS))}.")
    out: Dict[str, Any] = {}
    panel = channels.get("painel") or {}
    out["painel"] = {"porta": int(panel.get("porta", 0) or 0)}
    if not 0 <= out["painel"]["porta"] <= 65535:
        raise ManifestError("'canais.painel.porta' inválida.")
    for key, value in channels.items():
        if value is not None and not isinstance(value, dict):
            raise ManifestError(f"'canais.{key}' deve ser um objeto.")
    if channels.get("telegram") is not None:
        tg = channels["telegram"]
        out["telegram"] = {"token_segredo": _check_secret_name("canais.telegram.token_segredo", tg.get("token_segredo")),
                           "chat_ids_segredo": _check_secret_name("canais.telegram.chat_ids_segredo", tg.get("chat_ids_segredo"))}
        if not out["telegram"]["token_segredo"]:
            raise ManifestError("canais.telegram precisa de 'token_segredo' (o NOME do segredo com o token do bot).")
    if channels.get("api") is not None:
        api = channels["api"]
        out["api"] = {"token_segredo": _check_secret_name("canais.api.token_segredo", api.get("token_segredo")),
                      "requisicoes_por_minuto": max(1, int(api.get("requisicoes_por_minuto", 20)))}
        if not out["api"]["token_segredo"]:
            raise ManifestError("canais.api precisa de 'token_segredo' (o NOME do segredo com o token da API).")
    if channels.get("twitch") is not None:
        if kind != "vtuber":
            raise ManifestError("O canal twitch exige tipo 'vtuber'.")
        tw = dict(channels["twitch"])
        for key in ("token_segredo", "refresh_token_segredo", "client_id_segredo", "client_secret_segredo"):
            tw[key] = _check_secret_name(f"canais.twitch.{key}", tw.get(key))
        if not str(tw.get("canal") or "").strip() or not str(tw.get("nick") or "").strip() or not tw["token_segredo"]:
            raise ManifestError("canais.twitch precisa de 'canal', 'nick' e 'token_segredo'.")
        if tw.get("emocoes") is not None and not isinstance(tw["emocoes"], dict):
            raise ManifestError("canais.twitch.emocoes deve ser um objeto {emoção: nome do hotkey}.")
        out["twitch"] = tw
    if channels.get("overlay") is not None and channels["overlay"].get("ativo", True):
        if kind != "vtuber":
            raise ManifestError("O canal overlay (legenda para o OBS) exige tipo 'vtuber'.")
        out["overlay"] = {"ativo": True}
    m["canais"] = out
    return m


def load_manifest(source: Any) -> Dict[str, Any]:
    if isinstance(source, dict):
        return validate_manifest(source)
    path = Path(str(source)).expanduser()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as err:
        raise ManifestError(f"Manifesto não encontrado: {path}") from err
    except ValueError as err:
        raise ManifestError(f"{path.name} não é um JSON válido: {err}") from err
    return validate_manifest(raw)


BUILTIN_DIR = Path(__file__).resolve().parent / "agentes"


def search_dirs(extra: Optional[List[Path]] = None) -> List[Path]:
    """Onde procurar manifestos por id: pastas informadas, ./agents (seus agentes) e os exemplos do pacote."""
    dirs = [Path(d).expanduser() for d in (extra or [])] + [Path("agents"), BUILTIN_DIR]
    return list(dict.fromkeys(dirs))


def resolve_agent(ref: Any, dirs: Optional[List[Path]] = None) -> Dict[str, Any]:
    """Aceita um dicionário, um caminho de arquivo ou só o id ('kronus')."""
    if isinstance(ref, dict):
        return validate_manifest(ref)
    text = str(ref or "").strip()
    if not text:
        raise ManifestError("Informe o agente (id ou caminho do manifesto).")
    path = Path(text).expanduser()
    if path.suffix == ".json" or path.is_file():          # uma PASTA com o nome do agente não é manifesto
        return load_manifest(path)
    if not ID_RE.match(text.lower()):
        raise ManifestError(f"Agente '{text}' não encontrado.")
    for folder in search_dirs(dirs):
        candidate = folder / f"{text.lower()}.json"
        if candidate.is_file():
            return load_manifest(candidate)
    raise ManifestError(f"Agente '{text}' não encontrado em: {', '.join(str(d) for d in search_dirs(dirs))}.")


def catalog(dirs: Optional[List[Path]] = None) -> List[Dict[str, Any]]:
    """Agentes disponíveis (o primeiro encontrado vence quando dois arquivos têm o mesmo id)."""
    found: Dict[str, Dict[str, Any]] = {}
    for folder in search_dirs(dirs):
        if not folder.is_dir():
            continue
        for path in sorted(folder.glob("*.json")):
            try:
                m = load_manifest(path)
            except ManifestError:
                continue
            found.setdefault(m["id"], {"id": m["id"], "nome": m["nome"], "tipo": m["tipo"], "arquivo": str(path)})
    return sorted(found.values(), key=lambda a: a["nome"].lower())


def profile_of(manifest: Dict[str, Any]) -> str:
    """Perfil do motor que serve de base: 'trabalho' para agentes de trabalho, 'pessoal' para os demais."""
    return "trabalho" if manifest["tipo"] == "trabalho" else "pessoal"


def default_data_dir(manifest: Dict[str, Any], base: Optional[Path] = None) -> Path:
    return Path(base or (Path.home() / ".byte-agents")).expanduser() / manifest["id"]


# ---------------------------------------------------------------- prompt
SHORT_STYLE = ("Responda em no máximo 2 frases curtas, sem listas, sem markdown e sem blocos de código: a resposta pode "
               "ser lida numa tela pequena ou falada em voz alta.")


DEFAULT_PERSONAS = {
    "assistente": "Prestativo(a), direto(a) e cordial; resolve o pedido sem enrolação.",
    "trabalho": "Tom profissional, cordial e objetivo; apresenta a melhor solução com dados quando houver.",
    "vtuber": "Carismático(a), bem-humorado(a) e natural; conversa com o chat como gente de verdade.",
}


def persona_of(manifest: Dict[str, Any]) -> str:
    return str(manifest.get("persona") or DEFAULT_PERSONAS[manifest["tipo"]]).strip()


def identity_text(manifest: Dict[str, Any]) -> str:
    """Personalidade com o nome do agente na frente, sem repetir quando a persona já começa por "Você é <nome>"."""
    persona = persona_of(manifest)
    if engine_fold(persona[:80]).startswith(engine_fold(f"voce e {manifest['nome']}")):
        return persona
    return f"Você é {manifest['nome']}.\n{persona}"


def engine_fold(text: str) -> str:
    from . import engine
    return str(text).lower().translate(engine.TRANSLATION_TABLE)


_PLACEHOLDER_RE = re.compile(r"^(seu|sua) (nome|cargo|empresa|e-?mail)$|^nome da empresa$|\bseu nome\s*$", re.IGNORECASE)


def _is_placeholder(value: Any) -> bool:
    return bool(_PLACEHOLDER_RE.search(" ".join(str(value).split())))


def build_agent_prompt(manifest: Dict[str, Any]) -> str:
    """Prompt do agente. v12.2: apresentação corrigida (antes saía "Você é Byte, vTuber ..., de Seu Nome")."""
    from . import engine
    owner = manifest["dono"]
    owner_name = str(owner.get("usuario") or "").strip()
    descricao = str(manifest["descricao"]).strip().rstrip(".")
    head = f"Você é {manifest['nome']}. {descricao[:1].upper() + descricao[1:]}.\n"
    if owner_name:
        head += f"Você atende {owner_name}.\n"
    head += (f"QUEM VOCÊ É (mantenha essa personalidade em todas as respostas):\n{persona_of(manifest)}\n\n"
             f"1. SOBRE O USUÁRIO:\n{json.dumps(owner, ensure_ascii=False) if owner else '(não informado)'}\n\n")
    prompt = head + engine._PROMPT_TOOLS_SECTION + engine._PROMPT_GUIDELINES_SECTION
    if manifest["tipo"] == "trabalho":
        prompt += engine.WORK_GUIDELINES
    if manifest["estilo"]["respostas_curtas"]:
        prompt += "\n- " + SHORT_STYLE
    return prompt


# ---------------------------------------------------------------- aplicação ao motor
def apply_identity(manifest: Dict[str, Any]) -> None:
    """Ajusta as variáveis do motor para este agente. Chamar DEPOIS de engine.apply_profile()."""
    from . import engine
    engine.ACTIVE_AGENT = manifest
    engine.AGENT_NAME = manifest["nome"]
    engine.AGENT_PERSONA = identity_text(manifest)
    engine.OWNER_NAME = (manifest["dono"].get("usuario") or "você").split()[0]
    engine.UI_THEME = manifest["estilo"]["tema"]
    engine.SYSTEM_PROMPT_V100 = engine.SYSTEM_PROMPT_V90 = build_agent_prompt(manifest)
    engine.SYSTEM_PROMPT_TOOLS_TEXT_BLOCK = engine._PROMPT_TOOLS_SECTION


def disabled_tools(manifest: Dict[str, Any]) -> set:
    enabled = set(ALWAYS_ON_TOOLS)
    for skill in manifest["habilidades"]:
        enabled.update(SKILLS[skill])
    every = {tool for tools in SKILLS.values() for tool in tools}
    return every - enabled


def apply_to_service(service: Any, manifest: Dict[str, Any]) -> Any:
    """Liga só as habilidades do manifesto: remove ferramentas e ações das desligadas e configura as iniciativas."""
    blocked = disabled_tools(manifest)
    service.disabled_tools = set(blocked)
    for name in list(service.extra_tools):
        if name in blocked:
            service.extra_tools.pop(name, None)
    off_skills = set(SKILLS) - set(manifest["habilidades"])
    for skill in off_skills:
        for action in SKILL_ACTIONS.get(skill, ()):
            service.extra_actions.pop(action, None)
    if "web" in off_skills:
        service.status_providers.pop("web", None)
    engine_initiatives = service.proactive_engine
    engine_initiatives.autonomous_mode_enabled = manifest["iniciativas"]["ativas"]
    engine_initiatives.idle_threshold_sec = manifest["iniciativas"]["ociosidade_min"] * 60.0
    engine_initiatives.min_interval_sec = manifest["iniciativas"]["intervalo_min"] * 60.0
    if manifest["modelo"]:
        service.llm.apply_model_spec(manifest["modelo"])         # v13.3: aceita "azure:IMPLANTACAO" ou tag do Ollama
    service.agent_manifest = manifest
    service.profile_switcher = None                 # um agente definido por manifesto não troca de perfil
    return service


# ---------------------------------------------------------------- canais
def read_named_secret(name: str, read: Optional[Callable[[Tuple[str, ...]], str]] = None) -> str:
    if not name:
        return ""
    if read is None:
        from . import engine
        read = engine._read_secret
    return read((name,))


def stream_env(manifest: Dict[str, Any], read: Optional[Callable[[Tuple[str, ...]], str]] = None) -> Dict[str, str]:
    """Traduz canais.twitch do manifesto para as variáveis que o diretor da live já entende."""
    tw = manifest["canais"].get("twitch")
    if not tw:
        return {}
    env = {
        "BYTE_TWITCH_CHANNEL": str(tw["canal"]).strip(),
        "BYTE_TWITCH_NICK": str(tw["nick"]).strip(),
        "BYTE_TWITCH_TOKEN": read_named_secret(tw["token_segredo"], read),
        "BYTE_TWITCH_REFRESH_TOKEN": read_named_secret(tw.get("refresh_token_segredo", ""), read),
        "BYTE_TWITCH_CLIENT_ID": read_named_secret(tw.get("client_id_segredo", ""), read),
        "BYTE_TWITCH_CLIENT_SECRET": read_named_secret(tw.get("client_secret_segredo", ""), read),
        "BYTE_STREAM_NAME": manifest["nome"],
        "BYTE_STREAM_MODE": str(tw.get("modo") or "mention"),
        "BYTE_STREAM_IDLE_S": str(tw.get("fala_sozinha_s", 0)),
        "BYTE_STREAM_REPLY_IN_CHAT": "1" if tw.get("responder_no_chat", True) else "0",
    }
    if manifest["persona"]:
        env["BYTE_STREAM_PERSONA"] = identity_text(manifest)
    if tw.get("emocoes"):
        env["BYTE_STREAM_EMOTIONS"] = json.dumps(tw["emocoes"], ensure_ascii=False)
    for key, var in (("vts_url", "BYTE_VTS_URL"), ("tts", "BYTE_TTS_COMMAND"), ("player", "BYTE_AUDIO_PLAYER"),
                     ("parametro_boca", "BYTE_VTS_MOUTH_PARAM"), ("lista_bloqueio", "BYTE_STREAM_BLOCKLIST_FILE"),
                     ("ignorar_usuarios", "BYTE_STREAM_IGNORE_USERS")):
        if tw.get(key):
            env[var] = str(tw[key])
    return {k: v for k, v in env.items() if v != ""}


def persistent_token(path: Path) -> str:
    """Token aleatório guardado (0600) na pasta do agente, para URLs fixas como a da legenda no OBS."""
    path = Path(path)
    try:
        value = path.read_text(encoding="utf-8").strip()
        if re.fullmatch(r"[A-Za-z0-9_-]{32,}", value):
            return value
    except OSError:
        pass
    value = pysecrets.token_urlsafe(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(value)
    return value


# ---------------------------------------------------------------- modelos de manifesto
def template(agent_id: str, kind: str = "assistente", name: Optional[str] = None) -> Dict[str, Any]:
    if kind not in AGENT_TYPES:
        raise ManifestError(f"tipo deve ser um de: {', '.join(AGENT_TYPES)}.")
    prefix = re.sub(r"[^A-Z0-9]+", "_", agent_id.upper())
    data: Dict[str, Any] = {
        "id": agent_id,
        "nome": name or agent_id.capitalize(),
        "tipo": kind,
        "persona": "",
        "dono": {"usuario": "Seu Nome"},
        "habilidades": list(DEFAULT_SKILLS[kind]),
        "modelo": "",
        "iniciativas": {"ativas": kind == "assistente", "ociosidade_min": 10, "intervalo_min": 30},
        "estilo": {"tema": "pro" if kind == "trabalho" else "neon", "respostas_curtas": False},
        "canais": {"painel": {"porta": 0},
                   "telegram": {"token_segredo": f"{prefix}_TELEGRAM_TOKEN", "chat_ids_segredo": f"{prefix}_TELEGRAM_CHAT_IDS"},
                   "api": {"token_segredo": f"{prefix}_API_TOKEN"}},
    }
    if kind == "trabalho":
        data["dono"].update({"cargo": "Seu cargo", "empresa": "Sua empresa", "assinatura": "Atenciosamente,\nSeu Nome"})
    if kind == "vtuber":
        data["canais"]["twitch"] = {"canal": "seu_canal", "nick": "conta_do_bot", "token_segredo": f"{prefix}_TWITCH_TOKEN",
                                    "modo": "mention", "fala_sozinha_s": 0, "vts_url": "ws://127.0.0.1:8001",
                                    "emocoes": {"feliz": "Happy", "neutra": "Neutral"}}
        data["canais"]["overlay"] = {"ativo": True}
    validate_manifest(data)
    return data

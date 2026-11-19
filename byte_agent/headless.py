"""Modo servidor: roda a Byte fora do Colab (máquina própria, VPS ou Docker), sempre ligada.

    python -m byte_agent --data-dir ~/.byte-agent --port 8765

Configuração por variáveis de ambiente (veja .env.example):
    BYTE_DATA_DIR, OLLAMA_URL, BYTE_MODEL, BYTE_HOST, BYTE_PORT, BYTE_UI_TOKEN,
    BYTE_TELEGRAM_TOKEN, BYTE_TELEGRAM_CHAT_IDS
    Live: BYTE_TWITCH_CHANNEL, BYTE_TWITCH_NICK, BYTE_TWITCH_TOKEN, BYTE_VTS_URL, BYTE_TTS_COMMAND (ver README)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import agents, engine
from .speech import SpeechEngine
from .stream import StreamConfig, StreamDirector
from .telegram_bridge import TelegramBridge
from .twitch_bridge import TwitchAuth, TwitchChat
from .vtube_studio import VTubeStudioClient


def load_profile_data(data_dir: Path) -> dict:
    """Lê <data_dir>/perfil.json (usuario, cargo, empresa, assinatura...). Arquivo ausente ou inválido = perfil vazio."""
    try:
        data = json.loads((Path(data_dir).expanduser() / "perfil.json").read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def build_service(data_dir: Path, ollama_url: Optional[str] = None, model: Optional[str] = None, profile: Optional[str] = None,
                  web: Optional[bool] = None, profile_data: Optional[dict] = None) -> Any:
    """Monta banco, roteador, e-mail, terminal e serviço usando `data_dir` como armazenamento persistente.
    Com profile="trabalho" ativa o prompt profissional e instala as ferramentas de trabalho (uma instância por perfil)."""
    data_dir = Path(data_dir).expanduser()
    (data_dir / "database").mkdir(parents=True, exist_ok=True)
    if web is None:
        web = bool(profile)              # o modo servidor liga a web; chamadas diretas (testes) escolhem
    if profile_data is None:
        profile_data = load_profile_data(data_dir) if profile else {}
    if profile:
        engine.apply_profile(profile, profile_data)
    vec = engine.VectorEngine()
    db = engine.UnifiedDatabase(data_dir / "database" / "byte.db", vec,
                                drive_backup_path=data_dir / "database" / "byte_backup.db")
    llm = engine.TrueLLMAutonomousRouter(db=db, ollama_url=ollama_url or None)
    llm.mydrive_path = data_dir / "_sem_google_drive"        # fora do Colab não existe /content/drive
    if model:
        llm.apply_model_spec(str(model))                    # v13.0: "azure:IMPLANTACAO" (sem Ollama) ou tag do Ollama
        llm.base_model_spec = llm.current_model_spec()
    service = engine.ByteEcosystemService(db, llm, engine.RealEmailConnector(db),
                                          engine.MultiFrameGameTerminal(db, data_dir / "workspace"))
    if profile and engine.ACTIVE_PROFILE == "trabalho":
        from .work import WorkAssistant
        service.proactive_engine.autonomous_mode_enabled = False     # sem bate-papo espontâneo; lembretes e rotinas seguem
        WorkAssistant(service, data_dir, profile_data).install()
    if web:
        engine.install_web_access(service, data_dir)
    if profile:
        engine.install_integrations_and_push(service)
        from . import objetivos
        objetivos.install(service)                          # v13.9: objetivos de longo prazo, em etapas
        from . import gatilhos
        gatilhos.install(service)                           # v13.14: gatilhos por evento (agenda, e-mail, tarefas)
        from . import app_movel
        app_movel.install(service)                          # v13.16: aparelhos pareados (app do Kronus)
    return service


def build_avatar(env: Any, data_dir: Path) -> Tuple[Optional[VTubeStudioClient], Optional[SpeechEngine]]:
    """VTube Studio (BYTE_VTS_URL) e voz (BYTE_TTS_COMMAND), cada um opcional."""
    vts: Optional[VTubeStudioClient] = None
    speech: Optional[SpeechEngine] = None
    vts_url = env.get("BYTE_VTS_URL", "").strip()
    if vts_url:
        vts = VTubeStudioClient(vts_url, token_path=Path(data_dir).expanduser() / "vtube_studio_token.txt")
    tts_command = env.get("BYTE_TTS_COMMAND", "").strip()
    if tts_command:
        speech = SpeechEngine(tts_command, env.get("BYTE_AUDIO_PLAYER", ""), work_dir=Path(data_dir).expanduser() / "tts")
    return vts, speech


def build_stream(service: Any, env: Any, data_dir: Path) -> Optional[StreamDirector]:
    """Monta a live se BYTE_TWITCH_CHANNEL estiver definido. Devolve o diretor já ligado ao chat (ainda não iniciado)."""
    channel = env.get("BYTE_TWITCH_CHANNEL", "").strip()
    if not channel:
        return None
    data_dir = Path(data_dir).expanduser()
    config = StreamConfig.from_env(env, data_dir)
    auth = TwitchAuth(env.get("BYTE_TWITCH_TOKEN", ""), env.get("BYTE_TWITCH_REFRESH_TOKEN", ""),
                      env.get("BYTE_TWITCH_CLIENT_ID", ""), env.get("BYTE_TWITCH_CLIENT_SECRET", ""),
                      store_path=data_dir / "twitch_tokens.json")
    vts, speech = build_avatar(env, data_dir)
    director = StreamDirector(service.llm, config, vts=vts, speech=speech)
    director.chat = TwitchChat(env.get("BYTE_TWITCH_NICK", ""), auth, channel, on_message=director.on_chat)
    return director


def run_avatar_check(env: Any, data_dir: Path, say: str, list_hotkeys: bool) -> int:
    """Testa o avatar e a voz SEM Twitch e sem LLM: lista hotkeys e/ou fala uma frase com sincronia labial."""
    vts, speech = build_avatar(env, data_dir)
    if vts is None and speech is None:
        print("Defina BYTE_VTS_URL (ex.: ws://127.0.0.1:8001) e/ou BYTE_TTS_COMMAND antes de testar.")
        return 2
    if vts is not None:
        print("Conectando ao VTube Studio... se aparecer um aviso no VTube Studio, clique em Allow.")
        try:
            names = [h.get("name") for h in vts.hotkeys(refresh=True)]
            print(f"VTube Studio OK. Hotkeys do modelo atual: {names}")
        except Exception as err:
            print(f"VTube Studio: {err}")
            return 1
    if say:
        config = StreamConfig.from_env(env, data_dir)
        director = StreamDirector(llm=None, config=config, vts=vts, speech=speech)
        result = director.finalize_reply(say)
        if result is None:
            print("A frase foi descartada pelo filtro de saída.")
            return 1
        print(f"Resultado: {director._perform(result[0], result[1], mention=None, to_chat=False)}")
        if speech is not None and speech.last_error:
            print(f"Voz: {speech.last_error}")
    return 0


def parse_chat_ids(raw: str) -> List[int]:
    return [int(x) for x in re.findall(r"-?\d+", raw or "")]


def profile_data_dirs(data_dir: Path, profile: str, env: Any) -> Dict[str, Path]:
    """Pasta de cada perfil. A do perfil iniciado é --data-dir; a do outro vem de BYTE_WORK_DATA_DIR /
    BYTE_PERSONAL_DATA_DIR ou, por padrão, de uma pasta irmã (~/.byte-agent <-> ~/.byte-agent-trabalho)."""
    base = Path(data_dir).expanduser()
    current = engine.normalize_profile_name(profile)
    other = "trabalho" if current == "pessoal" else "pessoal"
    explicit = env.get("BYTE_WORK_DATA_DIR" if other == "trabalho" else "BYTE_PERSONAL_DATA_DIR", "").strip()
    if explicit:
        other_dir = Path(explicit).expanduser()
    elif other == "trabalho":
        other_dir = base.with_name(base.name + "-trabalho")
    elif base.name.endswith("-trabalho"):
        other_dir = base.with_name(base.name[: -len("-trabalho")])
    else:
        other_dir = base.with_name(base.name + "-pessoal")
    if other_dir.resolve() == base.resolve():
        raise SystemExit("As pastas dos perfis pessoal e de trabalho precisam ser diferentes.")
    return {current: base, other: other_dir}


class DataDirLock:
    """v12.5: impede dois processos de usarem a mesma pasta de dados (o mesmo banco SQLite). Ex.: o Kronus dedicado
    (`--agent kronus`) ligado e, ao mesmo tempo, a troca para o Kronus no painel principal."""

    def __init__(self, folder: Path):
        self.folder = Path(folder)
        self.path = self.folder / ".em-uso.lock"
        self._fh: Any = None

    def acquire(self) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+", encoding="utf-8")
        try:
            import fcntl
        except ImportError:                  # Windows sem WSL: sem trava (rode um agente por pasta)
            self._fh = fh
            return
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.seek(0)
            owner = fh.read().strip() or "outro processo"
            fh.close()
            raise RuntimeError(f"Os dados em {self.folder} já estão em uso ({owner}). "
                               "Esse agente já está ligado em outro processo: use o painel/página dele, ou desligue-o antes.") from None
        fh.seek(0)
        fh.truncate()
        fh.write(f"PID {os.getpid()}")
        fh.flush()
        self._fh = fh

    def release(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()                 # fechar o arquivo solta a trava
            finally:
                self._fh = None


class Runtime:
    """Serviço ativo + servidor + Telegram + live, com troca de perfil sem reiniciar o processo."""

    def __init__(self, args: argparse.Namespace, env: Any = None):
        self.args = args
        self.env = os.environ if env is None else env
        self.base_env = dict(self.env)
        # v12.3: troca de agentes (painel e /agente no Telegram) ligada por padrão; um processo dedicado a um agente
        # (--agent kronus) só troca com --agent-menu. BYTE_AGENT_MENU=0 desliga.
        self.agent_menu = bool(getattr(args, "agent_menu", False)) or (
            not getattr(args, "agent", "") and self.base_env.get("BYTE_AGENT_MENU", "") != "0")
        self.default_data_dir = Path.home() / ".byte-agent"
        # v12.0: com --agent, o manifesto define perfil, pasta, porta e canais; não há troca de perfil.
        self.manifest: Optional[dict] = agents.resolve_agent(args.agent) if getattr(args, "agent", None) else None
        if self.manifest is not None:
            args.profile = agents.profile_of(self.manifest)
            data_dir = Path(args.data_dir).expanduser() if getattr(args, "data_dir", None) else agents.default_data_dir(self.manifest)
            self.dirs = {args.profile: data_dir}
            self.agents_base = data_dir.parent
            self.default_dirs = profile_data_dirs(self.default_data_dir, "pessoal", self.base_env)
            merged = dict(self.env)
            merged.update(agents.stream_env(self.manifest))
            self.env = merged
            if not getattr(args, "port", None):
                args.port = self.manifest["canais"]["painel"]["porta"] or 0
        else:
            args.profile = engine.normalize_profile_name(getattr(args, "profile", "pessoal"))
            if not getattr(args, "data_dir", None):
                args.data_dir = str(Path.home() / ".byte-agent")
            if getattr(args, "port", None) is None:
                args.port = 8765
            self.dirs = profile_data_dirs(Path(args.data_dir), args.profile, self.env)
            self.default_dirs = dict(self.dirs)
            self.agents_base = Path(self.base_env.get("BYTE_AGENTS_DIR") or (Path(args.data_dir).expanduser().parent / ".byte-agents"))
        self.service: Any = None
        self.server: Any = None
        self.bridge: Optional[TelegramBridge] = None
        self.director: Optional[StreamDirector] = None
        self._lock = threading.Lock()
        self._dir_locks: Dict[Path, DataDirLock] = {}
        self.port = 0

    @property
    def data_dir(self) -> Path:
        return self.dirs[engine.ACTIVE_PROFILE]

    def _lock_dir(self, folder: Path) -> bool:
        """Trava a pasta de dados para este processo. Devolve True se a trava é nova (para soltar se o build falhar)."""
        key = Path(folder).expanduser().resolve()
        if key in self._dir_locks:
            return False
        lock = DataDirLock(key)
        lock.acquire()
        self._dir_locks[key] = lock
        return True

    def _unlock_dirs(self, keep: Optional[Path] = None) -> None:
        keep_key = Path(keep).expanduser().resolve() if keep is not None else None
        for key in list(self._dir_locks):
            if key != keep_key:
                self._dir_locks.pop(key).release()

    def _build(self, profile: str, model: Optional[str]) -> Any:
        fresh = self._lock_dir(self.dirs[profile])
        try:
            return self._build_unlocked(profile, model)
        except Exception:
            if fresh:
                self._dir_locks.pop(Path(self.dirs[profile]).expanduser().resolve()).release()
            raise

    def _build_unlocked(self, profile: str, model: Optional[str]) -> Any:
        if self.manifest is not None:
            service = build_service(self.dirs[profile], self.args.ollama_url, model, profile=profile,
                                    web="web" in self.manifest["habilidades"], profile_data=self.manifest["dono"])
            agents.apply_identity(self.manifest)
            agents.apply_to_service(service, self.manifest)     # o "modelo" do manifesto, se houver, vence o --model
        else:
            service = build_service(self.dirs[profile], self.args.ollama_url, model, profile=profile)
        if self.manifest is None or self.agent_menu:
            service.profile_switcher = self.switch
        if self.agent_menu:
            service.agent_switcher = self.switch_agent
        if self.args.no_autonomy:
            service.proactive_engine.autonomous_mode_enabled = False
        return service

    def _announce(self) -> None:
        if self.manifest is not None:
            print(f"[{self.manifest['nome'].upper()} | byte-agent v{engine.BYTE_VERSION}] Agente '{self.manifest['id']}' ({self.manifest['tipo']}) | "
                  f"habilidades: {', '.join(self.manifest['habilidades']) or 'nenhuma'} | dados em {self.data_dir}", flush=True)
        else:
            print(f"[BYTE v{engine.BYTE_VERSION}] Perfil ativo: {engine.PROFILE_LABELS[engine.ACTIVE_PROFILE]} | dados em {self.data_dir}", flush=True)
        if engine.ACTIVE_PROFILE == "trabalho":
            print(f"[BYTE] Planilhas em {self.data_dir / 'arquivos'} | entregas em {self.data_dir / 'entregas'}", flush=True)

    def _start_live(self) -> None:
        if engine.ACTIVE_PROFILE == "trabalho":
            if self.env.get("BYTE_TWITCH_CHANNEL", "").strip():
                print("[BYTE] Live desligada no perfil de trabalho (troque para o perfil Matrix ou para o agente Byte para ligá-la).", flush=True)
            return
        try:
            director = build_stream(self.service, self.env, self.data_dir)
        except (ValueError, OSError) as err:          # ex.: token da Twitch ausente: o agente sobe sem a live
            print(f"[AVISO] Live não iniciada: {err}", flush=True)
            director = None
        if director is None:
            return
        director.chat.start()
        director.start()
        self.director = director
        if self.manifest is not None and self.manifest["tipo"] == "vtuber":
            self.service.live_owner_sink = director.owner_says     # v12.3: voz do dono -> fala da VTuber na live
        if self.bridge is not None:
            self.bridge.stream_director = director
        parts = [f"Twitch #{director.chat.channel} (modo {director.cfg.respond_mode})",
                 "VTube Studio" if director.vts else "sem avatar", "voz" if director.speech else "sem voz"]
        print(f"[BYTE] Live ativa: {' | '.join(parts)}", flush=True)

    def _stop_live(self) -> None:
        director, self.director = self.director, None
        if self.service is not None:
            self.service.live_owner_sink = None
        if self.bridge is not None:
            self.bridge.stream_director = None
        if director is not None:
            director.stop()
            director.chat.stop()
            if director.vts is not None:
                director.vts.close()

    def _channel_settings(self, warn_telegram: bool = True) -> Dict[str, Any]:
        """Tokens e opções de canal: do manifesto (pelos NOMES dos segredos) ou dos argumentos de linha de comando."""
        out: Dict[str, Any] = {"telegram_token": self.args.telegram_token, "telegram_chat_ids": self.args.telegram_chat_ids,
                               "api_token": "", "api_rpm": 20, "overlay_token": ""}
        if self.manifest is None:
            return out
        channels = self.manifest["canais"]
        if "telegram" in channels:
            out["telegram_token"] = agents.read_named_secret(channels["telegram"]["token_segredo"])
            out["telegram_chat_ids"] = agents.read_named_secret(channels["telegram"]["chat_ids_segredo"])
            if not out["telegram_token"] and warn_telegram:   # na troca de agente o bot já ligado continua; não é erro
                print(f"[AVISO] Telegram configurado, mas o segredo {channels['telegram']['token_segredo']} está vazio.", flush=True)
        if "api" in channels:
            out["api_token"] = agents.read_named_secret(channels["api"]["token_segredo"])
            out["api_rpm"] = channels["api"]["requisicoes_por_minuto"]
            if out["api_token"] and len(out["api_token"]) < 24:
                raise SystemExit(f"O token da API ({channels['api']['token_segredo']}) precisa ter 24+ caracteres.")
            if not out["api_token"]:
                print(f"[AVISO] API configurada, mas o segredo {channels['api']['token_segredo']} está vazio: API desligada.", flush=True)
        if "overlay" in channels:
            out["overlay_token"] = agents.persistent_token(self.data_dir / "overlay_token.txt")
        return out

    def _overlay_state(self) -> Optional[Dict[str, Any]]:
        director = self.director
        return director.last_reply if director is not None else None

    def start(self) -> "Runtime":
        self.service = self._build(engine.normalize_profile_name(self.args.profile), self.args.model or None)
        self.service.llm.start_online_setup_async()
        channels = self._channel_settings()
        self.server = engine.ByteOmniServer(self.service, host=self.args.host, port=self.args.port,
                                            api_token=channels["api_token"] or None, api_requests_per_minute=channels["api_rpm"],
                                            overlay_token=channels["overlay_token"] or None, overlay_provider=self._overlay_state)
        if self.args.ui_token:
            if not re.fullmatch(r"[A-Za-z0-9_-]{24,}", self.args.ui_token):
                raise SystemExit("BYTE_UI_TOKEN precisa ter 24+ caracteres [A-Za-z0-9_-].")
            self.server.token = self.args.ui_token
        self.service.api_enabled = bool(channels["api_token"])     # v13.16: /parear só onde há app/API
        port = self.port = self.server.start()
        print(f"[BYTE v{engine.BYTE_VERSION}] Painel: http://{self.args.host}:{port}/ui?token={self.server.token}", flush=True)
        self._announce()
        if channels["api_token"]:
            print(f"[BYTE] API para clientes leves: POST http://{self.args.host}:{port}/api/v1/message (Authorization: Bearer <token>)", flush=True)
        if channels["overlay_token"]:
            print(f"[BYTE] Legenda para o OBS (fonte de navegador): http://127.0.0.1:{port}/overlay?token={channels['overlay_token']}", flush=True)
        if channels["telegram_token"]:
            self.bridge = TelegramBridge(self.service, channels["telegram_token"], parse_chat_ids(channels["telegram_chat_ids"]))
            self.bridge.start()
            if self.bridge.allowed:
                print(f"[BYTE] Telegram ativo para os chats: {sorted(self.bridge.allowed)}", flush=True)
            else:
                print("[BYTE] Telegram ativo SEM lista de permissão: o bot só informará o chat_id de quem escrever. "
                      "Defina BYTE_TELEGRAM_CHAT_IDS e reinicie.", flush=True)
        self._start_live()
        return self

    def switch(self, target: str, want_html: bool = False) -> Dict[str, Any]:
        """Troca Matrix (padrão) <-> Trabalho: desliga a live, sobe o serviço do outro perfil com o mesmo modelo
        e aponta painel e Telegram para ele. Chamado pelo painel (switch_profile) e por /perfil no Telegram."""
        target = engine.normalize_profile_name(target)
        if self.manifest is not None and self.agent_menu:      # v12.3: com um agente ativo, volta primeiro para a Matrix
            self.switch_agent("")
        with self._lock:
            if target != engine.ACTIVE_PROFILE:
                old = self.service
                previous = (engine.ACTIVE_PROFILE, dict(engine.ACTIVE_PROFILE_DATA))
                try:
                    new = self._build(target, None)
                except Exception:
                    engine.apply_profile(*previous)
                    raise
                self._stop_live()
                engine.transfer_runtime(old, new, servers=[self.server])
                self.service = new
                self._unlock_dirs(keep=self.dirs[target])
                if self.bridge is not None:
                    self.bridge.service = new
                engine.start_llm_like(old, new)
                self._announce()
                self._start_live()
            st = self.service.dispatch_request("status", {})
        if want_html:
            st["html"] = engine.build_v90_web_ui(st, token=self.server.token)
        return st

    def switch_agent(self, target: str, want_html: bool = False) -> Dict[str, Any]:
        """Troca o agente em execução (Byte VTuber, Bit, Kronus... ou a Matrix padrão, target="").
        Cada agente usa a própria pasta de dados. O Telegram continua no bot iniciado; a API e a legenda passam a usar
        os tokens do agente novo."""
        with self._lock:
            new_manifest = agents.resolve_agent(target) if str(target or "").strip() else None
            current_id = (self.manifest or {}).get("id", "")
            if (new_manifest or {}).get("id", "") != current_id:
                old = self.service
                previous = (engine.ACTIVE_PROFILE, dict(engine.ACTIVE_PROFILE_DATA), engine.ACTIVE_AGENT)
                old_state = (self.manifest, self.dirs, self.env)
                self.manifest = new_manifest
                if new_manifest is not None:
                    profile = agents.profile_of(new_manifest)
                    self.dirs = {profile: self.agents_base / new_manifest["id"]}
                    self.env = dict(self.base_env)
                    self.env.update(agents.stream_env(new_manifest))
                else:
                    profile = "pessoal"
                    self.dirs = dict(self.default_dirs)
                    self.env = dict(self.base_env)
                try:
                    new = self._build(profile, None)
                except Exception:
                    self.manifest, self.dirs, self.env = old_state
                    engine.apply_profile(previous[0], previous[1])
                    if previous[2]:
                        agents.apply_identity(previous[2])
                    raise
                self._stop_live()
                engine.transfer_runtime(old, new, servers=[self.server])
                new.profile_switcher = self.switch
                new.agent_switcher = self.switch_agent
                engine.apply_agent_model(new, new_manifest)
                self.service = new
                self._unlock_dirs(keep=self.dirs[profile])
                if self.bridge is not None:
                    self.bridge.service = new
                channels = self._channel_settings(warn_telegram=False)
                self.server.api_token = channels["api_token"] or None
                self.server.api_requests_per_minute = channels["api_rpm"]
                self.server.overlay_token = channels["overlay_token"] or None
                engine.start_llm_like(old, new)
                self._announce()
                self._start_live()
            st = self.service.dispatch_request("status", {})
        if want_html:
            st["html"] = engine.build_v90_web_ui(st, token=self.server.token)
        return st

    def stop(self) -> None:
        self._stop_live()
        if self.bridge is not None:
            self.bridge.stop()
        if self.server is not None:
            self.server.stop()
        if self.service is not None:
            self.service.shutdown()
        self._unlock_dirs()


def start_all(args: argparse.Namespace) -> Tuple[Any, Any, Optional[TelegramBridge], Optional[StreamDirector]]:
    """Compatibilidade: inicia tudo e devolve (serviço, servidor, Telegram, live) do perfil inicial."""
    runtime = Runtime(args).start()
    return runtime.service, runtime.server, runtime.bridge, runtime.director


def agent_commands(argv: List[str]) -> int:
    """novo-agente <id> [--tipo T] [--nome N] [--pasta agents] | validar-agente <arquivo.json>"""
    if argv[0] == "novo-agente":
        parser = argparse.ArgumentParser(prog="byte_agent novo-agente", description="Cria o manifesto de um novo agente.")
        parser.add_argument("id")
        parser.add_argument("--tipo", default="assistente", choices=sorted(agents.AGENT_TYPES))
        parser.add_argument("--nome", default=None)
        parser.add_argument("--pasta", default="agents")
        args = parser.parse_args(argv[1:])
        try:
            data = agents.template(args.id.lower(), args.tipo, args.nome)
        except agents.ManifestError as err:
            print(f"Erro: {err}")
            return 2
        target = Path(args.pasta) / f"{data['id']}.json"
        if target.exists():
            print(f"Erro: {target} já existe; não vou sobrescrever.")
            return 2
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"Manifesto criado: {target}\nEdite nome, persona e dono, crie os segredos listados e rode:\n"
              f"  python -m byte_agent validar-agente {target}\n  python -m byte_agent --agent {target}")
        return 0
    if len(argv) < 2:
        print("Use: python -m byte_agent validar-agente <arquivo.json>")
        return 2
    try:
        manifest = agents.load_manifest(argv[1])
    except agents.ManifestError as err:
        print(f"Manifesto inválido: {err}")
        return 1
    print(f"OK: {manifest['nome']} ({manifest['tipo']}) | habilidades: {', '.join(manifest['habilidades']) or 'nenhuma'}")
    print(f"Dados em: {agents.default_data_dir(manifest)} | canais: {', '.join(manifest['canais']) or 'painel'}")
    names = []
    for channel in manifest["canais"].values():
        names += [v for k, v in channel.items() if k.endswith("_segredo") and v]
    missing = [n for n in names if not agents.read_named_secret(n)]
    for name in names:
        print(f"  segredo {name}: {'FALTANDO' if name in missing else 'ok'}")
    return 1 if missing else 0


def main(argv: Optional[List[str]] = None) -> int:
    env = os.environ
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in {"novo-agente", "validar-agente"}:
        return agent_commands(argv)
    if argv and argv[0] == "avaliar":
        from .evaluate import main as evaluate_main
        return evaluate_main(argv[1:])
    if argv and argv[0] == "comportamento":
        from .comportamento import main as behavior_main
        return behavior_main(argv[1:])
    parser = argparse.ArgumentParser(prog="byte_agent", description="Byte em modo servidor (sem Colab).")
    parser.add_argument("--agent", default=env.get("BYTE_AGENT", ""),
                        help="manifesto JSON do agente (ex.: agents/kronus.json). Define perfil, pasta, porta e canais.")
    parser.add_argument("--data-dir", default=env.get("BYTE_DATA_DIR") or None)
    parser.add_argument("--agent-menu", action="store_true", default=env.get("BYTE_AGENT_MENU", "") == "1",
                        help="liga a troca de agentes (painel e /agente no Telegram) mesmo com --agent; sem --agent ela já vem ligada.")
    parser.add_argument("--profile", default=env.get("BYTE_PROFILE", "pessoal"), choices=["matrix", "pessoal", "trabalho"],
                        help="perfil inicial; dá para trocar depois pelo painel ou por /perfil no Telegram.")
    parser.add_argument("--ollama-url", default=env.get("OLLAMA_URL", ""))
    parser.add_argument("--model", default=env.get("BYTE_MODEL", ""))
    parser.add_argument("--host", default=env.get("BYTE_HOST", "127.0.0.1"),
                        help="Padrão 127.0.0.1. O painel permite executar código: não exponha à internet.")
    parser.add_argument("--port", type=int, default=int(env["BYTE_PORT"]) if env.get("BYTE_PORT") else None)
    parser.add_argument("--ui-token", default=env.get("BYTE_UI_TOKEN", ""))
    parser.add_argument("--telegram-token", default=env.get("BYTE_TELEGRAM_TOKEN", ""))
    parser.add_argument("--telegram-chat-ids", default=env.get("BYTE_TELEGRAM_CHAT_IDS", ""))
    parser.add_argument("--no-autonomy", action="store_true", help="Desliga as iniciativas espontâneas.")
    parser.add_argument("--vts-hotkeys", action="store_true", help="Só conecta ao VTube Studio, lista os hotkeys e sai.")
    parser.add_argument("--say", default="", help='Só testa avatar e voz com uma frase, ex.: --say "[feliz] Oi, chat!"')
    args = parser.parse_args(argv)

    if args.vts_hotkeys or args.say:
        avatar_env = dict(os.environ)
        if args.agent:
            avatar_env.update(agents.stream_env(agents.load_manifest(args.agent)))
        return run_avatar_check(avatar_env, Path(args.data_dir or (Path.home() / ".byte-agent")), args.say, args.vts_hotkeys)

    runtime = Runtime(args).start()
    stop = threading.Event()

    def _graceful(*_: Any) -> None:
        stop.set()

    signal.signal(signal.SIGINT, _graceful)
    signal.signal(signal.SIGTERM, _graceful)

    last_retry = time.monotonic()
    while not stop.wait(5.0):
        llm = runtime.service.llm
        director = runtime.director
        if director is not None and director.chat.fatal_error and not getattr(director, "_fatal_reported", False):
            director._fatal_reported = True
            print(f"[BYTE] Twitch: {director.chat.fatal_error}", flush=True)
        # o Ollama pode subir depois da Byte (docker compose, reboot): tenta de novo a cada 30 s
        if (llm.connection_mode == "online" and not llm.ollama_gpu_warm and not llm.provision_state.get("running")
                and time.monotonic() - last_retry >= 30.0):
            last_retry = time.monotonic()
            llm.start_online_setup_async()

    print("[BYTE] Encerrando...", flush=True)
    runtime.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())

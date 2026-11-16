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

from . import engine
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


def build_service(data_dir: Path, ollama_url: Optional[str] = None, model: Optional[str] = None, profile: Optional[str] = None) -> Any:
    """Monta banco, roteador, e-mail, terminal e serviço usando `data_dir` como armazenamento persistente.
    Com profile="trabalho" ativa o prompt profissional e instala as ferramentas de trabalho (uma instância por perfil)."""
    data_dir = Path(data_dir).expanduser()
    (data_dir / "database").mkdir(parents=True, exist_ok=True)
    profile_data = load_profile_data(data_dir) if profile else {}
    if profile:
        engine.apply_profile(profile, profile_data)
    vec = engine.VectorEngine()
    db = engine.UnifiedDatabase(data_dir / "database" / "byte.db", vec,
                                drive_backup_path=data_dir / "database" / "byte_backup.db")
    llm = engine.TrueLLMAutonomousRouter(db=db, ollama_url=ollama_url or None)
    llm.mydrive_path = data_dir / "_sem_google_drive"        # fora do Colab não existe /content/drive
    if model:
        llm.active_model = llm._require_valid_tag(model)[0]
    service = engine.ByteEcosystemService(db, llm, engine.RealEmailConnector(db),
                                          engine.MultiFrameGameTerminal(db, data_dir / "workspace"))
    if profile and engine.ACTIVE_PROFILE == "trabalho":
        from .work import WorkAssistant
        service.proactive_engine.autonomous_mode_enabled = False     # sem bate-papo espontâneo; lembretes e rotinas seguem
        WorkAssistant(service, data_dir, profile_data).install()
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


class Runtime:
    """Serviço ativo + servidor + Telegram + live, com troca de perfil sem reiniciar o processo."""

    def __init__(self, args: argparse.Namespace, env: Any = None):
        self.args = args
        self.env = os.environ if env is None else env
        self.dirs = profile_data_dirs(Path(args.data_dir), args.profile, self.env)
        self.service: Any = None
        self.server: Any = None
        self.bridge: Optional[TelegramBridge] = None
        self.director: Optional[StreamDirector] = None
        self._lock = threading.Lock()
        self.port = 0

    @property
    def data_dir(self) -> Path:
        return self.dirs[engine.ACTIVE_PROFILE]

    def _build(self, profile: str, model: Optional[str]) -> Any:
        service = build_service(self.dirs[profile], self.args.ollama_url, model, profile=profile)
        if self.args.no_autonomy:
            service.proactive_engine.autonomous_mode_enabled = False
        service.profile_switcher = self.switch
        return service

    def _announce(self) -> None:
        print(f"[BYTE v{engine.BYTE_VERSION}] Perfil ativo: {engine.PROFILE_LABELS[engine.ACTIVE_PROFILE]} | dados em {self.data_dir}", flush=True)
        if engine.ACTIVE_PROFILE == "trabalho":
            print(f"[BYTE] Planilhas em {self.data_dir / 'arquivos'} | entregas em {self.data_dir / 'entregas'}", flush=True)

    def _start_live(self) -> None:
        if engine.ACTIVE_PROFILE == "trabalho":
            if self.env.get("BYTE_TWITCH_CHANNEL", "").strip():
                print("[BYTE] Live desligada no perfil de trabalho (troque para VTuber para ligá-la).", flush=True)
            return
        director = build_stream(self.service, self.env, self.dirs["pessoal"])
        if director is None:
            return
        director.chat.start()
        director.start()
        self.director = director
        if self.bridge is not None:
            self.bridge.stream_director = director
        parts = [f"Twitch #{director.chat.channel} (modo {director.cfg.respond_mode})",
                 "VTube Studio" if director.vts else "sem avatar", "voz" if director.speech else "sem voz"]
        print(f"[BYTE] Live ativa: {' | '.join(parts)}", flush=True)

    def _stop_live(self) -> None:
        director, self.director = self.director, None
        if self.bridge is not None:
            self.bridge.stream_director = None
        if director is not None:
            director.stop()
            director.chat.stop()
            if director.vts is not None:
                director.vts.close()

    def start(self) -> "Runtime":
        self.service = self._build(engine.normalize_profile_name(self.args.profile), self.args.model or None)
        self.service.llm.start_online_setup_async()
        self.server = engine.ByteOmniServer(self.service, host=self.args.host, port=self.args.port)
        if self.args.ui_token:
            if not re.fullmatch(r"[A-Za-z0-9_-]{24,}", self.args.ui_token):
                raise SystemExit("BYTE_UI_TOKEN precisa ter 24+ caracteres [A-Za-z0-9_-].")
            self.server.token = self.args.ui_token
        port = self.port = self.server.start()
        print(f"[BYTE v{engine.BYTE_VERSION}] Painel: http://{self.args.host}:{port}/ui?token={self.server.token}", flush=True)
        self._announce()
        if self.args.telegram_token:
            self.bridge = TelegramBridge(self.service, self.args.telegram_token, parse_chat_ids(self.args.telegram_chat_ids))
            self.bridge.start()
            if self.bridge.allowed:
                print(f"[BYTE] Telegram ativo para os chats: {sorted(self.bridge.allowed)}", flush=True)
            else:
                print("[BYTE] Telegram ativo SEM lista de permissão: o bot só informará o chat_id de quem escrever. "
                      "Defina BYTE_TELEGRAM_CHAT_IDS e reinicie.", flush=True)
        self._start_live()
        return self

    def switch(self, target: str, want_html: bool = False) -> Dict[str, Any]:
        """Troca VTuber (pessoal) <-> Trabalho: desliga a live, sobe o serviço do outro perfil com o mesmo modelo
        e aponta painel e Telegram para ele. Chamado pelo painel (switch_profile) e por /perfil no Telegram."""
        target = engine.normalize_profile_name(target)
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
                if self.bridge is not None:
                    self.bridge.service = new
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


def start_all(args: argparse.Namespace) -> Tuple[Any, Any, Optional[TelegramBridge], Optional[StreamDirector]]:
    """Compatibilidade: inicia tudo e devolve (serviço, servidor, Telegram, live) do perfil inicial."""
    runtime = Runtime(args).start()
    return runtime.service, runtime.server, runtime.bridge, runtime.director


def main(argv: Optional[List[str]] = None) -> int:
    env = os.environ
    parser = argparse.ArgumentParser(prog="byte_agent", description="Byte em modo servidor (sem Colab).")
    parser.add_argument("--data-dir", default=env.get("BYTE_DATA_DIR", str(Path.home() / ".byte-agent")))
    parser.add_argument("--profile", default=env.get("BYTE_PROFILE", "pessoal"), choices=["pessoal", "trabalho"],
                        help="perfil inicial; dá para trocar depois pelo painel ou por /perfil no Telegram.")
    parser.add_argument("--ollama-url", default=env.get("OLLAMA_URL", ""))
    parser.add_argument("--model", default=env.get("BYTE_MODEL", ""))
    parser.add_argument("--host", default=env.get("BYTE_HOST", "127.0.0.1"),
                        help="Padrão 127.0.0.1. O painel permite executar código: não exponha à internet.")
    parser.add_argument("--port", type=int, default=int(env.get("BYTE_PORT", "8765")))
    parser.add_argument("--ui-token", default=env.get("BYTE_UI_TOKEN", ""))
    parser.add_argument("--telegram-token", default=env.get("BYTE_TELEGRAM_TOKEN", ""))
    parser.add_argument("--telegram-chat-ids", default=env.get("BYTE_TELEGRAM_CHAT_IDS", ""))
    parser.add_argument("--no-autonomy", action="store_true", help="Desliga as iniciativas espontâneas.")
    parser.add_argument("--vts-hotkeys", action="store_true", help="Só conecta ao VTube Studio, lista os hotkeys e sai.")
    parser.add_argument("--say", default="", help='Só testa avatar e voz com uma frase, ex.: --say "[feliz] Oi, chat!"')
    args = parser.parse_args(argv)

    if args.vts_hotkeys or args.say:
        return run_avatar_check(os.environ, Path(args.data_dir), args.say, args.vts_hotkeys)

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

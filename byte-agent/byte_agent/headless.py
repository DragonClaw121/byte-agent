"""Modo servidor: roda a Byte fora do Colab (máquina própria, VPS ou Docker), sempre ligada.

    python -m byte_agent --data-dir ~/.byte-agent --port 8765

Configuração por variáveis de ambiente (veja .env.example):
    BYTE_DATA_DIR, OLLAMA_URL, BYTE_MODEL, BYTE_HOST, BYTE_PORT, BYTE_UI_TOKEN,
    BYTE_TELEGRAM_TOKEN, BYTE_TELEGRAM_CHAT_IDS
"""
from __future__ import annotations

import argparse
import os
import re
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Any, List, Optional, Tuple

from . import engine
from .telegram_bridge import TelegramBridge


def build_service(data_dir: Path, ollama_url: Optional[str] = None, model: Optional[str] = None) -> Any:
    """Monta banco, roteador, e-mail, terminal e serviço usando `data_dir` como armazenamento persistente."""
    data_dir = Path(data_dir).expanduser()
    (data_dir / "database").mkdir(parents=True, exist_ok=True)
    vec = engine.VectorEngine()
    db = engine.UnifiedDatabase(data_dir / "database" / "byte.db", vec,
                                drive_backup_path=data_dir / "database" / "byte_backup.db")
    llm = engine.TrueLLMAutonomousRouter(db=db, ollama_url=ollama_url or None)
    llm.mydrive_path = data_dir / "_sem_google_drive"        # fora do Colab não existe /content/drive
    if model:
        llm.active_model = llm._require_valid_tag(model)[0]
    service = engine.ByteEcosystemService(db, llm, engine.RealEmailConnector(db),
                                          engine.MultiFrameGameTerminal(db, data_dir / "workspace"))
    return service


def parse_chat_ids(raw: str) -> List[int]:
    return [int(x) for x in re.findall(r"-?\d+", raw or "")]


def start_all(args: argparse.Namespace) -> Tuple[Any, Any, Optional[TelegramBridge]]:
    service = build_service(Path(args.data_dir), args.ollama_url, args.model)
    if args.no_autonomy:
        service.proactive_engine.autonomous_mode_enabled = False
    service.llm.start_online_setup_async()

    server = engine.ByteOmniServer(service, host=args.host, port=args.port)
    if args.ui_token:
        if not re.fullmatch(r"[A-Za-z0-9_-]{24,}", args.ui_token):
            raise SystemExit("BYTE_UI_TOKEN precisa ter 24+ caracteres [A-Za-z0-9_-].")
        server.token = args.ui_token
    port = server.start()
    print(f"[BYTE v{engine.BYTE_VERSION}] Painel: http://{args.host}:{port}/ui?token={server.token}", flush=True)

    bridge: Optional[TelegramBridge] = None
    if args.telegram_token:
        bridge = TelegramBridge(service, args.telegram_token, parse_chat_ids(args.telegram_chat_ids))
        bridge.start()
        if bridge.allowed:
            print(f"[BYTE] Telegram ativo para os chats: {sorted(bridge.allowed)}", flush=True)
        else:
            print("[BYTE] Telegram ativo SEM lista de permissão: o bot só informará o chat_id de quem escrever. "
                  "Defina BYTE_TELEGRAM_CHAT_IDS e reinicie.", flush=True)
    return service, server, bridge


def main(argv: Optional[List[str]] = None) -> int:
    env = os.environ
    parser = argparse.ArgumentParser(prog="byte_agent", description="Byte em modo servidor (sem Colab).")
    parser.add_argument("--data-dir", default=env.get("BYTE_DATA_DIR", str(Path.home() / ".byte-agent")))
    parser.add_argument("--ollama-url", default=env.get("OLLAMA_URL", ""))
    parser.add_argument("--model", default=env.get("BYTE_MODEL", ""))
    parser.add_argument("--host", default=env.get("BYTE_HOST", "127.0.0.1"),
                        help="Padrão 127.0.0.1. O painel permite executar código: não exponha à internet.")
    parser.add_argument("--port", type=int, default=int(env.get("BYTE_PORT", "8765")))
    parser.add_argument("--ui-token", default=env.get("BYTE_UI_TOKEN", ""))
    parser.add_argument("--telegram-token", default=env.get("BYTE_TELEGRAM_TOKEN", ""))
    parser.add_argument("--telegram-chat-ids", default=env.get("BYTE_TELEGRAM_CHAT_IDS", ""))
    parser.add_argument("--no-autonomy", action="store_true", help="Desliga as iniciativas espontâneas.")
    args = parser.parse_args(argv)

    service, server, bridge = start_all(args)
    stop = threading.Event()

    def _graceful(*_: Any) -> None:
        stop.set()

    signal.signal(signal.SIGINT, _graceful)
    signal.signal(signal.SIGTERM, _graceful)

    last_retry = time.monotonic()
    while not stop.wait(5.0):
        llm = service.llm
        # o Ollama pode subir depois da Byte (docker compose, reboot): tenta de novo a cada 30 s
        if (llm.connection_mode == "online" and not llm.ollama_gpu_warm and not llm.provision_state.get("running")
                and time.monotonic() - last_retry >= 30.0):
            last_retry = time.monotonic()
            llm.start_online_setup_async()

    print("[BYTE] Encerrando...", flush=True)
    if bridge:
        bridge.stop()
    server.stop()
    service.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())

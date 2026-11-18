"""Supervisor para rodar a Byte num contêiner (Azure Container Apps) — v13.6.

O disco de um contêiner é apagado a cada reinício, e o SQLite da Byte (modo WAL) não pode morar num compartilhamento de
rede. Então este supervisor:
  1. restaura os dados do armazenamento persistente (Azure Files montado em BYTE_DADOS_PERSISTENTES, padrão /mnt/dados);
  2. roda a instância principal (Matrix/Byte/Bit, porta 8765, só local) e o Kronus dedicado (porta 8772, aberto para a
     página /voz do celular pelo endereço HTTPS do contêiner);
  3. a cada BYTE_SINCRONIZAR_MIN minutos (padrão 5) e ao desligar, salva uma cópia consistente dos dados no
     armazenamento (bancos SQLite copiados pela API de backup, troca atômica do arquivo);
  4. religa um processo que cair;
  5. com BYTE_PAINEL_REMOTO=1, abre os painéis pelo endereço HTTPS do contêiner: um roteador na porta pública manda
     cada pedido para o painel dono do token (principal ou Kronus). Sem token válido, tudo segue para o Kronus como
     antes (só /voz e a API). Os tokens ficam fixos em painel-tokens.json no armazenamento, para o favorito não mudar.

    python -m byte_agent.nuvem

Para levar os dados do notebook: rode `byte backup` lá e envie o arquivo byte-AAAA-MM-DD_HHMM.tar.gz para o
armazenamento. Na primeira partida ele é restaurado; depois vale o byte-dados.tar.gz mantido pelo supervisor.
"""
from __future__ import annotations

import hmac
import http.client
import http.server
import io
import json
import os
import secrets
import shutil
import signal
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from pathlib import Path
import urllib.parse
from typing import Dict, List, Optional, Tuple

SNAPSHOT = "byte-dados.tar.gz"
DATA_DIRS = (".byte-agent", ".byte-agent-trabalho", ".byte-agents")
SKIP_SUFFIXES = ("-wal", "-shm", ".lock", ".pyc")
SKIP_DIRS = {"__pycache__", "_sem_google_drive"}


def log(msg: str) -> None:
    print(f"[NUVEM] {msg}", flush=True)


# ---------------------------------------------------------------- cópia e restauração dos dados
def _clean_name(name: str) -> str:
    """'./.byte-agent/x' -> '.byte-agent/x' (lstrip('./') comeria o ponto de '.byte-agent')."""
    while name.startswith("./"):
        name = name[2:]
    return name


def _safe_members(tar: tarfile.TarFile) -> List[tarfile.TarInfo]:
    """Só aceita arquivos e pastas dentro das pastas de dados da Byte (nada de ../ ou caminhos absolutos)."""
    ok = []
    for m in tar.getmembers():
        name = _clean_name(m.name)
        if m.name.startswith("/") or ".." in Path(name).parts or not (m.isfile() or m.isdir()):
            continue
        if not any(name == d or name.startswith(d + "/") for d in DATA_DIRS):
            continue
        ok.append(m)
    return ok


def restore(home: Path, store: Path) -> Optional[Path]:
    """Restaura o snapshot do supervisor; se ainda não existir, o backup mais novo gerado por `byte backup`."""
    candidates = [store / SNAPSHOT] if (store / SNAPSHOT).is_file() else sorted(
        store.glob("byte-20*.tar.gz"), key=lambda p: p.stat().st_mtime, reverse=True)[:1]
    if not candidates:
        log(f"Nenhum dado salvo em {store}: começando do zero.")
        return None
    source = candidates[0]
    with tarfile.open(source, "r:gz") as tar:
        members = _safe_members(tar)
        for m in members:
            target = home / _clean_name(m.name)
            if m.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            src = tar.extractfile(m)
            if src is not None:
                with open(target, "wb") as out:
                    shutil.copyfileobj(src, out)
    log(f"Dados restaurados de {source.name} ({len(members)} itens).")
    return source


def _add_file(tar: tarfile.TarFile, path: Path, arcname: str) -> None:
    if path.suffix == ".db" or path.name.endswith(".sqlite"):
        with tempfile.TemporaryDirectory() as tmp:            # cópia consistente mesmo com o banco aberto (WAL)
            copy = Path(tmp) / path.name
            src = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
            dst = sqlite3.connect(str(copy))
            try:
                src.backup(dst)
            finally:
                dst.close()
                src.close()
            tar.add(str(copy), arcname=arcname)
        return
    tar.add(str(path), arcname=arcname)


def snapshot(home: Path, store: Path) -> Optional[Path]:
    """Salva os dados em store/byte-dados.tar.gz (escreve num temporário e troca de uma vez; guarda a cópia anterior)."""
    store.mkdir(parents=True, exist_ok=True)
    buf = io.BytesIO()
    count = 0
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for d in DATA_DIRS:
            root = home / d
            if not root.is_dir():
                continue
            for path in sorted(root.rglob("*")):
                rel = path.relative_to(home)
                if any(p in SKIP_DIRS for p in rel.parts) or path.name.endswith(SKIP_SUFFIXES) or not path.is_file():
                    continue
                try:
                    _add_file(tar, path, str(rel))
                    count += 1
                except (OSError, sqlite3.Error) as err:
                    log(f"Aviso: {rel} não entrou na cópia ({type(err).__name__}).")
    if not count:
        return None
    final = store / SNAPSHOT
    tmp = store / (SNAPSHOT + ".novo")
    tmp.write_bytes(buf.getvalue())
    if final.exists():
        os.replace(final, store / (SNAPSHOT + ".anterior"))
    os.replace(tmp, final)
    return final


# ---------------------------------------------------------------- processos
KRONUS_INTERNAL_PORT = 8773


def remote_panel(env: Dict[str, str]) -> bool:
    return env.get("BYTE_PAINEL_REMOTO", "0") == "1"


def public_port(env: Dict[str, str]) -> int:
    return int(env.get("BYTE_KRONUS_PORT", "8772") or 8772)


def commands(env: Dict[str, str]) -> Dict[str, List[str]]:
    model = env.get("BYTE_MODEL", "").strip()
    if not model:
        raise SystemExit("[NUVEM] Defina BYTE_MODEL (ex.: azure:byte-mini) nas variáveis do contêiner.")
    extra = [] if env.get("BYTE_AUTONOMIA", "0") == "1" else ["--no-autonomy"]
    procs = {"principal": [sys.executable, "-m", "byte_agent", "--model", model, "--host", "127.0.0.1", "--port", "8765"] + extra}
    if env.get("BYTE_KRONUS", "1") != "0":
        # com o painel remoto, o Kronus fica só local e o roteador ocupa a porta pública
        host, port = ("127.0.0.1", KRONUS_INTERNAL_PORT) if remote_panel(env) else ("0.0.0.0", public_port(env))
        procs["kronus"] = [sys.executable, "-m", "byte_agent", "--agent", "kronus", "--model", model,
                           "--host", host, "--port", str(port)] + extra
    return procs


def panel_tokens(store: Path) -> Dict[str, str]:
    """Tokens fixos dos painéis (criados uma vez e guardados no armazenamento persistente)."""
    path = store / "painel-tokens.json"
    data: Dict[str, str] = {}
    try:
        data = {k: v for k, v in json.loads(path.read_text()).items() if isinstance(v, str) and len(v) >= 24}
    except (OSError, ValueError, AttributeError):
        pass
    if not {"principal", "kronus"} <= data.keys():
        data = {"principal": data.get("principal") or secrets.token_urlsafe(32),
                "kronus": data.get("kronus") or secrets.token_urlsafe(32)}
        try:
            store.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data))
            os.chmod(path, 0o600)
        except OSError as err:
            log(f"Aviso: não consegui guardar os tokens do painel ({type(err).__name__}); valem até reiniciar.")
    return data


_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer",
        "transfer-encoding", "upgrade", "host", "content-length"}


class PanelRouter:
    """Porta pública do contêiner: pedidos com o token de um painel vão para ele (como se viessem do próprio PC);
    o resto vai para o Kronus com o endereço original (a página /voz e a API continuam exigindo o token delas)."""

    MAX_BODY = 32 * 1024 * 1024

    def __init__(self, host: str, port: int, routes: Dict[str, int], default_port: int):
        self.routes = routes                         # token -> porta local
        self.default_port = default_port
        router = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            server_version = "byte-roteador"
            sys_version = ""

            def log_message(self, *_: object) -> None:      # não registra URLs (elas levam o token)
                pass

            def _forward(self) -> None:
                target, local = router.target(self.path, self.headers)
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    length = -1
                if length < 0 or length > router.MAX_BODY:
                    self._reply(413, {"error": "Corpo da requisição grande demais"})
                    return
                body = self.rfile.read(length) if length else None
                headers = {k: v for k, v in self.headers.items() if k.lower() not in _HOP}
                headers["Host"] = f"127.0.0.1:{target}" if local else (self.headers.get("Host") or "")
                if body is not None:
                    headers["Content-Length"] = str(len(body))
                conn = http.client.HTTPConnection("127.0.0.1", target, timeout=600)
                try:
                    conn.request(self.command, self.path, body=body, headers=headers)
                    resp = conn.getresponse()
                    data = resp.read()
                except (OSError, http.client.HTTPException):
                    self._reply(502, {"error": "A Byte ainda está ligando ou reiniciando; tente de novo em instantes."})
                    return
                finally:
                    conn.close()
                self.send_response(resp.status, resp.reason)
                for k, v in resp.getheaders():
                    if k.lower() not in _HOP:
                        self.send_header(k, v)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(data)

            def _reply(self, code: int, payload: Dict[str, str]) -> None:
                raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            do_GET = do_POST = do_HEAD = do_OPTIONS = do_PUT = do_DELETE = _forward

        self.server = http.server.ThreadingHTTPServer((host, port), Handler)
        self.server.daemon_threads = True

    def target(self, raw_path: str, headers: "http.client.HTTPMessage") -> Tuple[int, bool]:
        """(porta, é_painel). Só o token do painel decide; comparação em tempo constante."""
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(raw_path).query)
        candidates = [headers.get("X-Byte-Token") or ""] + query.get("token", [])[:1]
        for cand in candidates:
            if not cand:
                continue
            for token, port in self.routes.items():
                if hmac.compare_digest(cand.encode("utf-8"), token.encode("utf-8")):
                    return port, True
        return self.default_port, False

    def start(self) -> threading.Thread:
        thread = threading.Thread(target=self.server.serve_forever, name="roteador", daemon=True)
        thread.start()
        return thread

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def public_address(env: Dict[str, str]) -> str:
    name, suffix = env.get("CONTAINER_APP_NAME", ""), env.get("CONTAINER_APP_ENV_DNS_SUFFIX", "")
    return f"https://{name}.{suffix}" if name and suffix else "https://SEU-ENDERECO.azurecontainerapps.io"


class Supervisor:
    def __init__(self, home: Path, store: Path, workdir: Path, env: Dict[str, str], interval_s: float):
        self.home, self.store, self.workdir, self.env = home, store, workdir, env
        self.interval_s = interval_s
        self.children: Dict[str, subprocess.Popen] = {}
        self.child_env: Dict[str, Dict[str, str]] = {}
        self.router: Optional[PanelRouter] = None
        self.stopping = threading.Event()
        self._signal_stop = False        # sinal recebido (SIGTERM do Container Apps). Não mexa em Event dentro do
                                         # tratador de sinal: o Event usa um lock e pode travar o próprio processo.

    def _should_stop(self) -> bool:
        return self._signal_stop or self.stopping.is_set()

    def _pause(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while not self._should_stop() and time.monotonic() < end:
            time.sleep(min(0.5, max(0.0, end - time.monotonic())))

    def start_child(self, name: str, cmd: List[str]) -> None:
        log(f"Ligando {name}: {' '.join(c for c in cmd[1:])}")
        env = dict(self.env, **self.child_env.get(name, {}))
        self.children[name] = subprocess.Popen(cmd, cwd=str(self.workdir), env=env)

    def run(self) -> int:
        def _on_signal(*_: object) -> None:
            self._signal_stop = True
        if threading.current_thread() is threading.main_thread():
            signal.signal(signal.SIGTERM, _on_signal)
            signal.signal(signal.SIGINT, _on_signal)
        procs = commands(self.env)
        if remote_panel(self.env):
            self.open_panels(procs)
        for name, cmd in procs.items():
            self.start_child(name, cmd)
        next_sync = time.monotonic() + self.interval_s
        while not self._should_stop():
            for name, proc in list(self.children.items()):
                if proc.poll() is not None and not self._should_stop():
                    log(f"{name} parou (código {proc.returncode}); religando em 10 s.")
                    self._pause(10)
                    if not self._should_stop():
                        self.start_child(name, procs[name])
            if time.monotonic() >= next_sync:
                self.sync()
                next_sync = time.monotonic() + self.interval_s
            self._pause(2)
        return self.shutdown()

    def open_panels(self, procs: Dict[str, List[str]]) -> None:
        tokens = panel_tokens(self.store)
        routes = {tokens["principal"]: 8765}
        self.child_env["principal"] = {"BYTE_UI_TOKEN": tokens["principal"]}
        if "kronus" in procs:
            routes[tokens["kronus"]] = KRONUS_INTERNAL_PORT
            self.child_env["kronus"] = {"BYTE_UI_TOKEN": tokens["kronus"]}
        try:
            self.router = PanelRouter("0.0.0.0", public_port(self.env), routes, KRONUS_INTERNAL_PORT)
        except OSError as err:
            log(f"Aviso: o roteador dos painéis não abriu ({err}); os painéis ficam só locais.")
            return
        self.router.start()
        base = public_address(self.env)
        log(f"Painel remoto ligado. Painel principal: {base}/ui?token={tokens['principal']}")
        if "kronus" in procs:
            log(f"Painel do Kronus: {base}/ui?token={tokens['kronus']}  |  página de voz: {base}/voz")

    def sync(self) -> None:
        try:
            path = snapshot(self.home, self.store)
            if path:
                log(f"Dados salvos ({path.stat().st_size // 1024} KB).")
        except Exception as err:                                  # o armazenamento falhar não pode derrubar a Byte
            log(f"Aviso: não consegui salvar os dados agora ({type(err).__name__}: {str(err)[:120]}).")

    def shutdown(self) -> int:
        log("Desligando: encerrando os agentes e salvando os dados.")
        for proc in self.children.values():
            if proc.poll() is None:
                proc.send_signal(signal.SIGINT)
        deadline = time.monotonic() + 20
        for proc in self.children.values():
            try:
                proc.wait(timeout=max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                proc.kill()
        if self.router is not None:
            self.router.stop()
        self.sync()
        return 0


def main() -> int:
    env = dict(os.environ)
    home = Path(env.get("HOME") or "/root")
    store = Path(env.get("BYTE_DADOS_PERSISTENTES", "/mnt/dados"))
    workdir = Path(env.get("BYTE_PASTA_TRABALHO", "/app"))
    workdir.mkdir(parents=True, exist_ok=True)
    if (store / "agents").is_dir():                              # seus agentes personalizados (ex.: agents/bit.json)
        shutil.copytree(store / "agents", workdir / "agents", dirs_exist_ok=True)
    restore(home, store)
    interval = max(10.0, float(env.get("BYTE_SINCRONIZAR_MIN", "5") or 5) * 60.0)
    return Supervisor(home, store, workdir, env, interval).run()


if __name__ == "__main__":
    raise SystemExit(main())

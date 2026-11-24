"""Supervisor para rodar a Byte num contêiner (Azure Container Apps) — v13.6.

O disco de um contêiner é apagado a cada reinício, e o SQLite da Byte (modo WAL) não pode morar num compartilhamento de
rede. Então este supervisor:
  1. restaura os dados do armazenamento persistente (Azure Files montado em BYTE_DADOS_PERSISTENTES, padrão /mnt/dados);
  2. roda a instância principal (Matrix/Byte/Bit, porta 8765, só local) e o Kronus dedicado (porta 8772, aberto para a
     página /voz do celular pelo endereço HTTPS do contêiner);
  3. a cada BYTE_SINCRONIZAR_MIN minutos (padrão 5) e ao desligar, salva uma cópia consistente dos dados no
     armazenamento (bancos SQLite copiados pela API de backup, troca atômica do arquivo);
  4. religa um processo que cair;
  6. com BYTE_GITHUB_REPO=dono/repositorio, atualiza sozinho: ao ligar e a cada BYTE_GITHUB_INTERVALO_H horas (padrão
     1 desde a v13.57; 0 = só ao ligar) procura o commit mais novo do branch (BYTE_GITHUB_BRANCH, padrão main) cujos testes PASSARAM
     no GitHub, baixa, instala e reinicia os agentes. Commit com teste falhando nunca é instalado. A cópia baixada
     fica no armazenamento (byte-agent-github.zip): se o GitHub estiver fora do ar, a Byte liga com ela.
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
import json
import os
import re
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
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import date
from typing import Any, Callable, Dict, List, Optional, Tuple

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


BACKUP_DIR = "copias"                                         # v13.57: cópias diárias e semanais
KEEP_DAILY, KEEP_WEEKLY = 7, 4


def _readable(path: Path) -> bool:
    """O .tar.gz abre e lista até o fim (um arquivo cortado ao meio falha aqui, não no meio da restauração)."""
    try:
        with tarfile.open(path, "r:gz") as tar:
            return bool(_safe_members(tar))
    except (OSError, tarfile.TarError, EOFError):
        return False


def restore_candidates(store: Path) -> List[Path]:
    """Em ordem de preferência: a cópia atual, a anterior (5 min antes), as diárias/semanais e os `byte backup`."""
    found = [store / SNAPSHOT, store / (SNAPSHOT + ".anterior")]
    folder = store / BACKUP_DIR
    if folder.is_dir():
        found += sorted(folder.glob("byte-dados-*.tar.gz"), key=lambda p: p.name, reverse=True)
    found += sorted(store.glob("byte-20*.tar.gz"), key=lambda p: p.stat().st_mtime, reverse=True)
    return [p for p in found if p.is_file()]


def restore(home: Path, store: Path) -> Optional[Path]:
    """Restaura a cópia mais nova que esteja inteira (v13.57: se a principal estiver corrompida, usa a anterior, depois as
    diárias); sem nenhuma, o backup mais novo gerado por `byte backup`."""
    candidates = restore_candidates(store)
    if not candidates:
        log(f"Nenhum dado salvo em {store}: começando do zero.")
        return None
    source = next((c for c in candidates if _readable(c)), None)
    if source is None:
        log("⚠️ Nenhuma cópia dos dados abre (todas corrompidas?): começando do zero, sem apagar as cópias.")
        return None
    if source != candidates[0]:
        log(f"⚠️ {candidates[0].name} está corrompida: restaurando de {source.name}.")
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


def snapshot(home: Path, store: Path, now: Optional[float] = None) -> Optional[Path]:
    """Salva os dados em store/byte-dados.tar.gz. v13.57: monta a cópia num arquivo temporário do disco local (não mais
    na memória: com 1 GiB e 4 agentes, a cópia em memória era um pico perigoso), confere se abre e só então troca;
    guarda a anterior e, uma vez por dia, uma cópia em copias/ (7 diárias + 4 semanais)."""
    store.mkdir(parents=True, exist_ok=True)
    count = 0
    with tempfile.TemporaryDirectory(prefix="byte-copia-") as work:
        local = Path(work) / SNAPSHOT
        with tarfile.open(local, mode="w:gz") as tar:
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
        if not _readable(local):
            log("⚠️ A cópia nova saiu corrompida: mantive a anterior.")
            return None
        final = store / SNAPSHOT
        tmp = store / (SNAPSHOT + ".novo")
        shutil.copyfile(local, tmp)
        if final.exists():
            os.replace(final, store / (SNAPSHOT + ".anterior"))
        os.replace(tmp, final)
    try:
        keep_daily(store, final, now)
    except OSError as err:
        log(f"Aviso: cópia diária não saiu ({type(err).__name__}).")
    return final


def keep_daily(store: Path, final: Path, now: Optional[float] = None) -> Optional[Path]:
    """Uma cópia por dia em copias/byte-dados-AAAA-MM-DD.tar.gz; guarda as 7 últimas e, das mais velhas, uma por semana
    (as 4 últimas semanas). Assim um erro que apague dados não leva junto todas as cópias em 10 minutos."""
    stamp = time.localtime(time.time() if now is None else now)
    folder = store / BACKUP_DIR
    folder.mkdir(parents=True, exist_ok=True)
    today = folder / f"byte-dados-{time.strftime('%Y-%m-%d', stamp)}.tar.gz"
    made = None
    if not today.exists():
        tmp = folder / (today.name + ".novo")
        shutil.copyfile(final, tmp)
        os.replace(tmp, today)
        made = today
    days = sorted(folder.glob("byte-dados-*.tar.gz"), key=lambda p: p.name, reverse=True)
    keep = set(days[:KEEP_DAILY])
    weeks: Dict[str, Path] = {}
    for p in days[KEEP_DAILY:]:
        try:
            year, week, _ = date(*time.strptime(p.name[11:21], "%Y-%m-%d")[:3]).isocalendar()
        except ValueError:
            continue
        weeks.setdefault(f"{year}-{week}", p)                  # a mais nova de cada semana
    keep |= set(list(weeks.values())[:KEEP_WEEKLY])
    for p in days:
        if p not in keep:
            p.unlink(missing_ok=True)
    return made


def list_backups(store: Path) -> List[Dict[str, Any]]:
    folder = store / BACKUP_DIR
    items = sorted(folder.glob("byte-dados-*.tar.gz"), key=lambda p: p.name, reverse=True) if folder.is_dir() else []
    return [{"dia": p.name[11:21], "kb": p.stat().st_size // 1024} for p in items]


# ---------------------------------------------------------------- processos
KRONUS_INTERNAL_PORT = 8773


def remote_panel(env: Dict[str, str]) -> bool:
    return env.get("BYTE_PAINEL_REMOTO", "0") == "1"


def public_port(env: Dict[str, str]) -> int:
    return int(env.get("BYTE_KRONUS_PORT", "8772") or 8772)


# v13.32 (R0.3): cada agente no seu processo, para todos trabalharem ao mesmo tempo (antes Bit e Byte só funcionavam
# quando "ativos" no processo da Matrix). BYTE_AGENTES escolhe quem roda à parte: padrão "kronus"; ex.: "kronus,bit".
# A Byte roda à parte sempre (v13.57), a menos que BYTE_BYTE=0.
SEPARATE_AGENTS = {"kronus": KRONUS_INTERNAL_PORT, "bit": 8774, "byte": 8775}


def separate_agents(env: Dict[str, str]) -> List[str]:
    raw = env.get("BYTE_AGENTES", "kronus")
    wanted = [a.strip().lower() for a in raw.replace(";", ",").split(",") if a.strip()]
    if env.get("BYTE_KRONUS", "1") == "0":
        wanted = [a for a in wanted if a != "kronus"]
    # v13.57: a Byte sempre tem a instância dela (painel, API e /u/byte), mesmo sem bot próprio do Telegram: sem
    # VTUBER_TELEGRAM_TOKEN o Telegram dela fica desligado (nunca usa o bot da Matrix). BYTE_BYTE=0 desliga.
    if env.get("BYTE_BYTE", "1") != "0" and "byte" not in wanted:
        wanted.append("byte")
    return [a for a in SEPARATE_AGENTS if a in wanted]


def commands(env: Dict[str, str]) -> Dict[str, List[str]]:
    model = env.get("BYTE_MODEL", "").strip()
    if not model:
        raise SystemExit("[NUVEM] Defina BYTE_MODEL (ex.: azure:byte-mini) nas variáveis do contêiner.")
    extra = [] if env.get("BYTE_AUTONOMIA", "0") == "1" else ["--no-autonomy"]
    procs = {"principal": [sys.executable, "-m", "byte_agent", "--model", model, "--host", "127.0.0.1", "--port", "8765"] + extra}
    for agent in separate_agents(env):
        if agent == "kronus" and not remote_panel(env):
            host, port = "0.0.0.0", public_port(env)     # sem o roteador, o Kronus atende direto na porta pública
        else:
            host, port = "127.0.0.1", SEPARATE_AGENTS[agent]
        procs[agent] = [sys.executable, "-m", "byte_agent", "--agent", agent, "--model", model,
                        "--host", host, "--port", str(port)] + extra
    return procs


def child_envs(env: Dict[str, str], procs: Dict[str, List[str]]) -> Dict[str, Dict[str, str]]:
    """Variáveis próprias de cada processo. A Matrix sabe quem roda à parte (não troca para eles). A Byte separada usa
    um bot do Telegram PRÓPRIO (VTUBER_TELEGRAM_*): o manifesto dela aponta para o bot da Matrix, e dois processos lendo
    o mesmo bot derrubam um ao outro."""
    out: Dict[str, Dict[str, str]] = {"principal": {"BYTE_AGENTES_SEPARADOS": ",".join(a for a in procs if a != "principal")}}
    if "byte" in procs:
        out["byte"] = {"BYTE_TELEGRAM_TOKEN": env.get("VTUBER_TELEGRAM_TOKEN", ""),
                       "BYTE_TELEGRAM_CHAT_IDS": env.get("VTUBER_TELEGRAM_CHAT_IDS", "")}
    return out


def panel_tokens(store: Path) -> Dict[str, str]:
    """Tokens fixos dos painéis (criados uma vez e guardados no armazenamento persistente)."""
    path = store / "painel-tokens.json"
    data: Dict[str, str] = {}
    try:
        data = {k: v for k, v in json.loads(path.read_text()).items() if isinstance(v, str) and len(v) >= 24}
    except (OSError, ValueError, AttributeError):
        pass
    wanted = {"principal", *SEPARATE_AGENTS}
    if not wanted <= data.keys():
        data = {name: data.get(name) or secrets.token_urlsafe(32) for name in sorted(wanted)}
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

    def __init__(self, host: str, port: int, routes: Dict[str, int], default_port: int,
                 paths: Optional[Dict[str, int]] = None):
        self.routes = routes                         # token -> porta local
        self.default_port = default_port
        self.paths = dict(paths or {})               # v13.42: "/u/<agente>" (painel pessoal de convidados) -> porta
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
        """(porta, é_painel). Só o token do painel decide; comparação em tempo constante. /u/<agente> vai para o agente
        (sem ser tratado como painel do dono: lá dentro, o código pessoal do convidado é quem dá acesso)."""
        split = urllib.parse.urlsplit(raw_path)
        for prefix, port in self.paths.items():
            if split.path == prefix or split.path.startswith(prefix + "/"):
                return port, False
        query = urllib.parse.parse_qs(split.query)
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


# ---------------------------------------------------------------- atualização pelo GitHub
GITHUB_API = "https://api.github.com"
GITHUB_ZIP = "byte-agent-github.zip"
GITHUB_VERSION_FILE = "github-versao.txt"
MAX_ZIP_BYTES = 50 * 1024 * 1024
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class GitHubUpdater:
    """Instala o commit mais novo do branch que passou nos testes do GitHub Actions."""

    def __init__(self, repo: str, store: Path, branch: str = "main", token: str = "", workflow: str = "testes",
                 api: str = GITHUB_API, installer: Optional[Callable[[Path], None]] = None):
        if not _REPO_RE.match(repo or ""):
            raise ValueError("BYTE_GITHUB_REPO deve ser dono/repositorio (ex.: DragonClaw121/byte-agent).")
        self.repo, self.store, self.branch, self.token = repo, store, branch or "main", token.strip()
        self.workflow, self.api = workflow, api.rstrip("/")
        self.installer = installer or self._pip_install
        self.last_error: Optional[str] = None

    def _get(self, url: str, timeout: float = 60.0, limit: int = 2 * 1024 * 1024) -> bytes:
        headers = {"Accept": "application/vnd.github+json", "User-Agent": "byte-agent-atualizador/1.0",
                   "X-GitHub-Api-Version": "2022-11-28"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout) as resp:
            data = resp.read(limit + 1)
        if len(data) > limit:
            raise ValueError("resposta grande demais")
        return data

    def installed(self) -> str:
        try:
            return (self.store / GITHUB_VERSION_FILE).read_text().strip()
        except OSError:
            return ""

    def latest_green(self) -> Optional[str]:
        query = urllib.parse.urlencode({"branch": self.branch, "status": "success", "event": "push", "per_page": 20})
        data = json.loads(self._get(f"{self.api}/repos/{self.repo}/actions/runs?{query}"))
        for run in data.get("workflow_runs") or []:
            if self.workflow and run.get("name") != self.workflow:
                continue
            sha = str(run.get("head_sha") or "")
            if _SHA_RE.match(sha):
                return sha
        return None

    def download(self, sha: str) -> Path:
        raw = self._get(f"{self.api}/repos/{self.repo}/zipball/{sha}", timeout=180, limit=MAX_ZIP_BYTES)
        tmp = self.store / (GITHUB_ZIP + ".novo")
        tmp.write_bytes(raw)
        try:
            with zipfile.ZipFile(tmp) as zf:
                names = zf.namelist()
            tops = {n.split("/", 1)[0] for n in names}
            if len(tops) != 1 or not any(n.endswith("/pyproject.toml") and n.count("/") == 1 for n in names) \
                    or not any(n.endswith("/byte_agent/nuvem.py") for n in names):
                raise ValueError("o arquivo baixado não parece a Byte")
        except (zipfile.BadZipFile, ValueError):
            tmp.unlink(missing_ok=True)
            raise
        final = self.store / GITHUB_ZIP
        os.replace(tmp, final)
        os.utime(final)                                  # o mais novo: é ele que a partida do contêiner instala
        return final

    @staticmethod
    def _pip_install(zip_path: Path) -> None:
        base = [sys.executable, "-m", "pip", "install", "-q", "--no-cache-dir", "--disable-pip-version-check"]
        proc = subprocess.run(base + [f"{zip_path}[trabalho]"], capture_output=True, text=True, timeout=900)
        if proc.returncode != 0:
            proc = subprocess.run(base + [str(zip_path)], capture_output=True, text=True, timeout=900)
        if proc.returncode != 0:
            raise RuntimeError("pip falhou: " + (proc.stderr or proc.stdout)[-300:])

    def check_and_install(self) -> Optional[str]:
        """Instala se houver versão aprovada mais nova. Devolve o sha instalado, ou None se nada mudou."""
        try:
            sha = self.latest_green()
            if not sha or sha == self.installed():
                self.last_error = None
                return None
            log(f"Atualização: nova versão aprovada nos testes ({sha[:7]}); baixando do GitHub...")
            zip_path = self.download(sha)
            self.installer(zip_path)
            (self.store / GITHUB_VERSION_FILE).write_text(sha + "\n")
            self.last_error = None
            log(f"Atualização: versão {sha[:7]} instalada.")
            return sha
        except (OSError, ValueError, RuntimeError, zipfile.BadZipFile, urllib.error.URLError,
                subprocess.TimeoutExpired) as err:
            self.last_error = f"{type(err).__name__}: {str(err)[:160]}"
            log(f"Aviso: não consegui atualizar pelo GitHub agora ({self.last_error}); sigo com a versão atual.")
            return None


def updater_from_env(env: Dict[str, str], store: Path) -> Optional[GitHubUpdater]:
    repo = env.get("BYTE_GITHUB_REPO", "").strip()
    if not repo:
        return None
    try:
        return GitHubUpdater(repo, store, branch=env.get("BYTE_GITHUB_BRANCH", "main").strip(),
                             token=env.get("BYTE_GITHUB_TOKEN", ""), workflow=env.get("BYTE_GITHUB_WORKFLOW", "testes").strip())
    except ValueError as err:
        log(f"Aviso: {err} A atualização automática fica desligada.")
        return None


UPDATE_FLAG = "atualizar-agora"


UPDATE_RESULT = "resultado-atualizacao.json"


def request_update(env: Dict[str, str], agent: str = "matrix") -> Dict[str, Any]:
    """v13.17: /atualizar (Telegram, app ou painel). O agente roda em outro processo; deixa um sinal no armazenamento que
    o supervisor confere a cada volta. v13.41: o sinal diz QUEM pediu, e o supervisor grava o resultado para esse
    agente responder no próprio bot (antes só a Matrix avisava, e "sem novidade" não tinha resposta)."""
    store = env.get("BYTE_DADOS_PERSISTENTES", "").strip()
    if not store or not env.get("BYTE_GITHUB_REPO", "").strip():
        raise PermissionError("A atualização pelo GitHub só existe na nuvem (BYTE_GITHUB_REPO).")
    at = time.time()
    Path(store, UPDATE_FLAG).write_text(json.dumps({"at": at, "agent": agent}))
    return {"text": "🔄 Conferindo o GitHub agora. Se houver versão nova aprovada nos testes, instalo e reinicio "
                    "(2 a 3 minutos). Eu respondo aqui de qualquer jeito.", "at": at}


def read_update_result(store: Path) -> Dict[str, Any]:
    try:
        return json.loads((Path(store) / UPDATE_RESULT).read_text())
    except (OSError, ValueError):
        return {}


def restart_supervisor() -> None:
    """Troca o processo do supervisor pela versão recém-instalada (mesmo PID, o contêiner nem percebe)."""
    os.execv(sys.executable, [sys.executable, "-m", "byte_agent.nuvem"])


def public_address(env: Dict[str, str]) -> str:
    custom = env.get("BYTE_ENDERECO", "").strip().rstrip("/")      # v13.42: domínio próprio (ex.: https://assistente.suaempresa.com.br)
    if custom:
        return custom if custom.startswith("http") else "https://" + custom
    name, suffix = env.get("CONTAINER_APP_NAME", ""), env.get("CONTAINER_APP_ENV_DNS_SUFFIX", "")
    return f"https://{name}.{suffix}" if name and suffix else "https://SEU-ENDERECO.azurecontainerapps.io"


class Supervisor:
    def __init__(self, home: Path, store: Path, workdir: Path, env: Dict[str, str], interval_s: float,
                 updater: Optional[GitHubUpdater] = None, update_interval_s: float = 0.0,
                 restart: Callable[[], None] = restart_supervisor):
        self.home, self.store, self.workdir, self.env = home, store, workdir, env
        self.interval_s = interval_s
        self.updater, self.update_interval_s, self.restart = updater, update_interval_s, restart
        self._update_thread: Optional[threading.Thread] = None
        self._update_ready: Optional[str] = None
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
        for name, extra in child_envs(self.env, procs).items():
            self.child_env.setdefault(name, {}).update(extra)
        if remote_panel(self.env):
            self.open_panels(procs)
        for name, cmd in procs.items():
            self.start_child(name, cmd)
        next_sync = time.monotonic() + self.interval_s
        next_update = time.monotonic() + self.update_interval_s
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
            flag = self.store / UPDATE_FLAG
            if self.updater is not None and flag.exists():
                try:
                    request = json.loads(flag.read_text() or "{}")
                except (OSError, ValueError):
                    request = {}
                try:
                    flag.unlink()
                except OSError:
                    pass
                log(f"Atualização pedida por {request.get('agent') or 'um agente'} (/atualizar).")
                self._check_update_in_background(request if isinstance(request, dict) else {})
            if self.updater is not None and self.update_interval_s > 0 and time.monotonic() >= next_update:
                next_update = time.monotonic() + self.update_interval_s
                self._check_update_in_background()
            if self._update_ready:
                log(f"Reiniciando os agentes na versão nova ({self._update_ready[:7]}).")
                self.shutdown()
                self.restart()
                return 0
            self._pause(2)
        return self.shutdown()

    def _check_update_in_background(self, request: Optional[Dict[str, Any]] = None) -> None:
        th = self._update_thread
        if th is not None and th.is_alive():
            return

        def work() -> None:
            sha = self.updater.check_and_install() if self.updater else None
            if request:                                  # v13.41: o resultado volta para quem pediu
                error = str(getattr(self.updater, "last_error", "") or "")
                status = "atualizando" if sha else ("erro" if error else "sem_novidade")
                result = {"agent": request.get("agent"), "at": request.get("at"), "status": status, "sha": sha or "",
                          "error": error, "done_at": time.time()}
                try:
                    (self.store / UPDATE_RESULT).write_text(json.dumps(result))
                except OSError:
                    pass
            if sha:
                self._update_ready = sha

        self._update_thread = threading.Thread(target=work, daemon=True, name="atualizador")
        self._update_thread.start()

    def open_panels(self, procs: Dict[str, List[str]]) -> None:
        tokens = panel_tokens(self.store)
        routes = {tokens["principal"]: 8765}
        self.child_env.setdefault("principal", {})["BYTE_UI_TOKEN"] = tokens["principal"]
        for agent in procs:
            if agent in SEPARATE_AGENTS:
                routes[tokens[agent]] = SEPARATE_AGENTS[agent]
                self.child_env.setdefault(agent, {})["BYTE_UI_TOKEN"] = tokens[agent]
        # v13.40: a Matrix abre o painel de quem roda à parte (menu de agentes e /painel)
        base = public_address(self.env)
        self.child_env["principal"]["BYTE_PAINEIS"] = json.dumps(
            {a: f"{base}/ui?token={tokens[a]}" for a in procs if a in SEPARATE_AGENTS})
        paths = {"/u/matrix": 8765, "/pc/matrix": 8765}   # v13.42: painéis pessoais; v13.47: app do PC
        self.child_env["principal"]["BYTE_LINK_PESSOAS"] = f"{base}/u/matrix"
        for agent in procs:
            if agent in SEPARATE_AGENTS:
                paths[f"/u/{agent}"] = SEPARATE_AGENTS[agent]
                paths[f"/pc/{agent}"] = SEPARATE_AGENTS[agent]
                self.child_env.setdefault(agent, {})["BYTE_LINK_PESSOAS"] = f"{base}/u/{agent}"
        try:
            self.router = PanelRouter("0.0.0.0", public_port(self.env), routes, KRONUS_INTERNAL_PORT, paths)
        except OSError as err:
            log(f"Aviso: o roteador dos painéis não abriu ({err}); os painéis ficam só locais.")
            return
        self.router.start()
        base = public_address(self.env)
        log(f"Painel remoto ligado. Painel principal: {base}/ui?token={tokens['principal']}")
        if "kronus" in procs:
            log(f"Painel do Kronus: {base}/ui?token={tokens['kronus']}  |  página de voz: {base}/voz")
        for agent in ("bit", "byte"):
            if agent in procs:
                log(f"Painel {'do Bit' if agent == 'bit' else 'da Byte'}: {base}/ui?token={tokens[agent]}")

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


def set_timezone(env: Dict[str, str]) -> Optional[str]:
    """v13.14: o contêiner nasce em UTC; lembretes e agenda precisam do horário de Brasília (BYTE_FUSO)."""
    name = env.get("BYTE_FUSO") or "America/Sao_Paulo"
    if env.get("TZ"):
        return env["TZ"]
    if not Path("/usr/share/zoneinfo", name).is_file():
        log(f"Aviso: o fuso {name} não está instalado no contêiner; os horários ficam em UTC.")
        return None
    env["TZ"] = os.environ["TZ"] = name
    if hasattr(time, "tzset"):
        time.tzset()
    return name


def main() -> int:
    env = dict(os.environ)
    set_timezone(env)
    home = Path(env.get("HOME") or "/root")
    store = Path(env.get("BYTE_DADOS_PERSISTENTES", "/mnt/dados"))
    env["BYTE_DADOS_PERSISTENTES"] = str(store)     # v13.18: os agentes precisam saber (/atualizar, /avaliar)
    workdir = Path(env.get("BYTE_PASTA_TRABALHO", "/app"))
    workdir.mkdir(parents=True, exist_ok=True)
    if (store / "agents").is_dir():                              # seus agentes personalizados (ex.: agents/bit.json)
        shutil.copytree(store / "agents", workdir / "agents", dirs_exist_ok=True)
    updater = updater_from_env(env, store)
    if updater is not None and updater.check_and_install():
        restart_supervisor()                                     # liga já com a versão nova (não volta daqui)
    restore(home, store)
    interval = max(10.0, float(env.get("BYTE_SINCRONIZAR_MIN", "5") or 5) * 60.0)
    try:
        hours = float(env.get("BYTE_GITHUB_INTERVALO_H", "1") or 0)
    except ValueError:
        hours = 1.0
    update_every = max(0.25, hours) * 3600.0 if hours > 0 else 0.0
    return Supervisor(home, store, workdir, env, interval, updater=updater, update_interval_s=update_every).run()


if __name__ == "__main__":
    raise SystemExit(main())

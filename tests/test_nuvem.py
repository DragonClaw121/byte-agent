"""v13.4: Byte em contêiner (Azure Container Apps): dados persistentes e supervisor."""
import json
import io
import sqlite3
import sys
import tarfile
import threading
import time
from pathlib import Path

from byte_agent import nuvem


def make_home(home: Path) -> sqlite3.Connection:
    data = home / ".byte-agent"
    data.mkdir(parents=True)
    conn = sqlite3.connect(str(data / "byte.db"))
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("CREATE TABLE memorias (texto TEXT)")
    conn.execute("INSERT INTO memorias VALUES ('gosta de café')")
    conn.commit()
    (data / "overlay_token.txt").write_text("abc")
    (data / ".em-uso.lock").write_text("PID 1")
    (home / ".byte-agents" / "kronus").mkdir(parents=True)
    (home / ".byte-agents" / "kronus" / "notas.txt").write_text("oi")
    return conn


def test_snapshot_and_restore_with_the_database_open(tmp_path):
    home, store, new_home = tmp_path / "home", tmp_path / "store", tmp_path / "novo"
    conn = make_home(home)                                     # banco aberto, com dados ainda no -wal
    path = nuvem.snapshot(home, store)
    conn.execute("INSERT INTO memorias VALUES ('mora em SP')")
    conn.commit()
    nuvem.snapshot(home, store)
    conn.close()
    assert path == store / nuvem.SNAPSHOT and (store / (nuvem.SNAPSHOT + ".anterior")).is_file()
    with tarfile.open(path) as tar:
        names = tar.getnames()
    assert ".byte-agent/byte.db" in names and not any(n.endswith(("-wal", "-shm", ".lock")) for n in names)
    nuvem.restore(new_home, store)
    rows = sqlite3.connect(str(new_home / ".byte-agent" / "byte.db")).execute("SELECT texto FROM memorias").fetchall()
    assert rows == [("gosta de café",), ("mora em SP",)]
    assert (new_home / ".byte-agents" / "kronus" / "notas.txt").read_text() == "oi"


def test_first_start_restores_the_notebook_backup_and_ignores_unsafe_entries(tmp_path):
    store, home = tmp_path / "store", tmp_path / "home"
    store.mkdir()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:                    # formato do `byte backup` do notebook
        for name, data in {".byte-agent/byte.db": b"SQLite", "../../etc/cron.d/x": b"mal", ".bashrc": b"mal",
                           ".byte-agents/bit/dados.txt": b"ok"}.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    (store / "byte-2026-11-18_1200.tar.gz").write_bytes(buf.getvalue())
    assert nuvem.restore(home, store).name == "byte-2026-11-18_1200.tar.gz"
    assert (home / ".byte-agent" / "byte.db").read_bytes() == b"SQLite"
    assert (home / ".byte-agents" / "bit" / "dados.txt").is_file()
    assert not (home / ".bashrc").exists() and not (tmp_path / "etc").exists()
    assert nuvem.restore(tmp_path / "vazio", tmp_path / "sem-dados") is None


def test_commands_need_a_model_and_open_only_the_kronus_port():
    import pytest
    with pytest.raises(SystemExit, match="BYTE_MODEL"):
        nuvem.commands({})
    cmds = nuvem.commands({"BYTE_MODEL": "azure:byte-mini", "BYTE_AUTONOMIA": "1"})
    assert cmds["principal"][-4:] == ["--host", "127.0.0.1", "--port", "8765"]
    assert "--agent" in cmds["kronus"] and cmds["kronus"][cmds["kronus"].index("--host") + 1] == "0.0.0.0"
    assert "--no-autonomy" not in cmds["kronus"]
    assert "kronus" not in nuvem.commands({"BYTE_MODEL": "x", "BYTE_KRONUS": "0"})


def test_supervisor_restarts_crashed_processes_and_saves_on_stop(tmp_path, monkeypatch):
    home, store = tmp_path / "home", tmp_path / "store"
    make_home(home).close()
    monkeypatch.setattr(nuvem, "commands", lambda env: {
        "principal": [sys.executable, "-c", "import time; time.sleep(60)"],
        "kronus": [sys.executable, "-c", "raise SystemExit(3)"]})            # cai logo
    sup = nuvem.Supervisor(home, store, tmp_path, {}, interval_s=3600)
    monkeypatch.setattr(sup, "_pause", lambda seconds: time.sleep(min(seconds, 0.2)))
    started = []
    real_start = sup.start_child
    sup.start_child = lambda name, cmd: (started.append(name), real_start(name, cmd))
    threading.Timer(2.0, sup.stopping.set).start()
    assert sup.run() == 0
    assert started.count("kronus") >= 2 and started.count("principal") == 1      # religou só o que caiu
    assert (store / nuvem.SNAPSHOT).is_file()                                     # salvou ao desligar
    assert all(p.poll() is not None for p in sup.children.values())



def test_sigterm_from_the_platform_stops_children_and_saves(tmp_path):
    """O Container Apps pede para desligar com SIGTERM; antes da correção o supervisor podia travar aqui."""
    import signal
    import subprocess
    home, store = tmp_path / "home", tmp_path / "store"
    make_home(home).close()
    code = (
        "import sys; from pathlib import Path; from byte_agent import nuvem\n"
        "nuvem.commands = lambda env: {'principal': [sys.executable, '-c', 'import time; time.sleep(120)']}\n"
        f"sys.exit(nuvem.Supervisor(Path({str(home)!r}), Path({str(store)!r}), Path({str(tmp_path)!r}), {{}}, 3600).run())\n")
    proc = subprocess.Popen([sys.executable, "-c", code], cwd=str(Path(nuvem.__file__).parents[1]),
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    time.sleep(2.0)
    proc.send_signal(signal.SIGTERM)
    out, _ = proc.communicate(timeout=30)
    assert proc.returncode == 0 and "Desligando" in out and "Dados salvos" in out
    assert (store / nuvem.SNAPSHOT).is_file()


def _backend(name):
    """Servidor local mínimo que responde com o próprio nome, o Host recebido e o corpo."""
    import http.server
    import json as _json

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _answer(self):
            n = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(n).decode() if n else ""
            raw = _json.dumps({"who": name, "host": self.headers.get("Host"), "path": self.path, "body": body}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(raw)
        do_GET = do_POST = _answer

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_panel_router_sends_each_token_to_its_panel_and_the_rest_to_kronus():
    import http.client
    import json as _json
    principal, kronus = _backend("principal"), _backend("kronus")
    tp, tk = "p" * 43, "k" * 43
    router = nuvem.PanelRouter("127.0.0.1", 0, {tp: principal.server_port, tk: kronus.server_port}, kronus.server_port)
    router.start()
    port = router.server.server_port

    def call(method, path, headers=None, body=None):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        c.request(method, path, body=body, headers=dict({"Host": "byte.exemplo.azurecontainerapps.io"}, **(headers or {})))
        r = c.getresponse()
        data = _json.loads(r.read())
        c.close()
        return r.status, data, r

    try:
        st, d, r = call("GET", f"/ui?token={tp}")
        assert d["who"] == "principal" and d["host"] == f"127.0.0.1:{principal.server_port}"
        assert r.getheader("X-Content-Type-Options") == "nosniff"
        st, d, _ = call("POST", "/api/dispatch", {"X-Byte-Token": tp, "Content-Type": "application/json"}, '{"a":1}')
        assert d["who"] == "principal" and d["body"] == '{"a":1}'
        st, d, _ = call("GET", f"/ui?token={tk}")
        assert d["who"] == "kronus" and d["host"].startswith("127.0.0.1:")
        # sem token de painel (ou token errado): Kronus com o endereço original -> o painel continua bloqueado lá
        for path in ("/voz", "/ui?token=errado", "/api/v1/status"):
            st, d, _ = call("GET", path)
            assert d["who"] == "kronus" and d["host"] == "byte.exemplo.azurecontainerapps.io"
    finally:
        router.stop()
        principal.shutdown()
        kronus.shutdown()
    st_err = nuvem.PanelRouter("127.0.0.1", 0, {}, 1)        # destino fora do ar -> 502 com mensagem clara
    st_err.start()
    try:
        c = http.client.HTTPConnection("127.0.0.1", st_err.server.server_port, timeout=10)
        c.request("GET", "/voz")
        r = c.getresponse()
        assert r.status == 502 and "ligando" in r.read().decode()
    finally:
        st_err.stop()


def test_remote_panel_keeps_kronus_local_and_tokens_stable(tmp_path):
    env = {"BYTE_MODEL": "azure:byte-mini", "BYTE_PAINEL_REMOTO": "1"}
    k = nuvem.commands(env)["kronus"]
    assert k[k.index("--host") + 1] == "127.0.0.1" and k[k.index("--port") + 1] == str(nuvem.KRONUS_INTERNAL_PORT)
    first = nuvem.panel_tokens(tmp_path)
    assert first == nuvem.panel_tokens(tmp_path) and first["principal"] != first["kronus"]
    assert oct((tmp_path / "painel-tokens.json").stat().st_mode)[-3:] == "600"
    assert nuvem.public_address({"CONTAINER_APP_NAME": "byte", "CONTAINER_APP_ENV_DNS_SUFFIX": "x.brazilsouth.azurecontainerapps.io"}) \
        == "https://byte.x.brazilsouth.azurecontainerapps.io"


# ------------------------------------------------------------------ v13.10: atualização pelo GitHub
GREEN = "a" * 40
RED = "b" * 40


def _zip_bytes(top="DragonClaw121-byte-agent-aaaaaaa", byte=True):
    buf = io.BytesIO()
    import zipfile
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(f"{top}/pyproject.toml", "[project]\nname='byte-agent'\n")
        if byte:
            zf.writestr(f"{top}/byte_agent/nuvem.py", "# supervisor\n")
    return buf.getvalue()


class FakeGitHub:
    def __init__(self, runs=None, archive=None):
        import http.server
        import json as _json
        self.runs = runs if runs is not None else [{"name": "outro", "head_sha": RED}, {"name": "testes", "head_sha": GREEN}]
        self.archive = archive if archive is not None else _zip_bytes()
        self.calls, self.fail = [], False
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                outer.calls.append(self.path)
                if outer.fail:
                    self.send_response(503); self.send_header("Content-Length", "0"); self.end_headers(); return
                if "/actions/runs?" in self.path:
                    assert "status=success" in self.path and "branch=main" in self.path
                    raw = _json.dumps({"workflow_runs": outer.runs}).encode()
                elif "/zipball/" in self.path:
                    raw = outer.archive
                else:
                    self.send_response(404); self.send_header("Content-Length", "0"); self.end_headers(); return
                self.send_response(200); self.send_header("Content-Length", str(len(raw))); self.end_headers()
                self.wfile.write(raw)

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_port}"


def test_updater_installs_only_the_newest_commit_that_passed_the_tests(tmp_path):
    gh = FakeGitHub()
    installed = []
    up = nuvem.GitHubUpdater("DragonClaw121/byte-agent", tmp_path, api=gh.url, installer=installed.append)
    try:
        assert up.latest_green() == GREEN                                  # ignora outro workflow
        assert up.check_and_install() == GREEN
        assert installed == [tmp_path / nuvem.GITHUB_ZIP] and (tmp_path / nuvem.GITHUB_VERSION_FILE).read_text().strip() == GREEN
        assert any(f"/zipball/{GREEN}" in c for c in gh.calls)
        assert up.check_and_install() is None and len(installed) == 1     # já instalada: nada muda
        gh.runs = []                                                        # nada aprovado: nada instala
        (tmp_path / nuvem.GITHUB_VERSION_FILE).write_text("")
        assert up.check_and_install() is None and len(installed) == 1
    finally:
        gh.srv.shutdown()


def test_updater_rejects_strange_archives_and_survives_github_outages(tmp_path):
    gh = FakeGitHub(archive=_zip_bytes(byte=False))
    installed = []
    up = nuvem.GitHubUpdater("DragonClaw121/byte-agent", tmp_path, api=gh.url, installer=installed.append)
    try:
        assert up.check_and_install() is None and installed == [] and "não parece a Byte" in up.last_error
        assert not (tmp_path / nuvem.GITHUB_VERSION_FILE).exists() and not (tmp_path / nuvem.GITHUB_ZIP).exists()
        gh.archive = b"isto nao e um zip"
        assert up.check_and_install() is None and "BadZipFile" in up.last_error
        gh.fail = True
        assert up.check_and_install() is None and "HTTP" in up.last_error or "503" in up.last_error
        def broken(_):
            raise RuntimeError("pip falhou: sem rede")
        gh.fail, gh.archive = False, _zip_bytes()
        up.installer = broken
        assert up.check_and_install() is None and not (tmp_path / nuvem.GITHUB_VERSION_FILE).exists()   # tenta de novo depois
    finally:
        gh.srv.shutdown()
    assert nuvem.updater_from_env({}, tmp_path) is None
    assert nuvem.updater_from_env({"BYTE_GITHUB_REPO": "nao e repo; rm -rf /"}, tmp_path) is None
    assert nuvem.updater_from_env({"BYTE_GITHUB_REPO": "DragonClaw121/byte-agent"}, tmp_path).branch == "main"


def test_supervisor_restarts_itself_when_a_new_version_is_installed(tmp_path, monkeypatch):
    home, store = tmp_path / "home", tmp_path / "store"
    make_home(home).close()
    monkeypatch.setattr(nuvem, "commands", lambda env: {"principal": [sys.executable, "-c", "import time; time.sleep(60)"]})

    class StubUpdater:
        def __init__(self):
            self.calls = 0
        def check_and_install(self):
            self.calls += 1
            return GREEN

    restarted = []
    sup = nuvem.Supervisor(home, store, tmp_path, {}, interval_s=3600, updater=StubUpdater(), update_interval_s=0.5,
                           restart=lambda: restarted.append(True))
    monkeypatch.setattr(sup, "_pause", lambda seconds: time.sleep(min(seconds, 0.2)))
    threading.Timer(15.0, sup.stopping.set).start()                     # trava de segurança do teste
    assert sup.run() == 0
    assert restarted == [True] and sup.updater.calls == 1
    assert all(p.poll() is not None for p in sup.children.values())      # agentes encerrados antes de reiniciar
    assert (store / nuvem.SNAPSHOT).is_file()                            # e os dados salvos


def test_update_on_request_from_telegram(tmp_path, monkeypatch):
    import pytest
    home, store = tmp_path / "home", tmp_path / "store"
    make_home(home).close()
    store.mkdir()
    with pytest.raises(PermissionError, match="só existe na nuvem"):
        nuvem.request_update({})
    out = nuvem.request_update({"BYTE_DADOS_PERSISTENTES": str(store), "BYTE_GITHUB_REPO": "DragonClaw121/byte-agent"})
    assert "Conferindo o GitHub" in out["text"] and (store / nuvem.UPDATE_FLAG).exists()
    monkeypatch.setattr(nuvem, "commands", lambda env: {"principal": [sys.executable, "-c", "import time; time.sleep(60)"]})

    class Stub:
        calls = 0
        def check_and_install(self):
            Stub.calls += 1
            return None                                                   # nada novo: não reinicia

    sup = nuvem.Supervisor(home, store, tmp_path, {}, interval_s=3600, updater=Stub(), update_interval_s=0,
                           restart=lambda: (_ for _ in ()).throw(AssertionError("não devia reiniciar")))
    monkeypatch.setattr(sup, "_pause", lambda seconds: time.sleep(min(seconds, 0.2)))
    threading.Timer(2.0, sup.stopping.set).start()
    assert sup.run() == 0
    assert Stub.calls == 1 and not (store / nuvem.UPDATE_FLAG).exists()   # conferiu uma vez e apagou o sinal


def test_new_version_is_announced_once(tmp_path, monkeypatch):
    from byte_agent import engine, headless
    db = engine.UnifiedDatabase(tmp_path / "byte.db", engine.VectorEngine())
    svc = type("S", (), {"db": db})()
    assert headless.announce_version(svc) is False                       # 1ª vez: só registra
    assert headless.announce_version(svc) is False                       # mesma versão: nada
    db.set_setting("versao_anunciada", "13.0")
    assert headless.announce_version(svc) is True
    msg = db.pop_undelivered_proactive_messages("telegram")[-1]["message"]
    assert msg == f"✅ Atualizei: v13.0 → v{engine.BYTE_VERSION}."


def test_each_agent_in_its_own_process():
    """v13.32 (R0.3): BYTE_AGENTES escolhe quem roda à parte; a Byte separada ganha bot do Telegram próprio."""
    env = {"BYTE_MODEL": "azure:byte-mini", "BYTE_PAINEL_REMOTO": "1", "BYTE_AGENTES": "kronus, bit,byte",
           "VTUBER_TELEGRAM_TOKEN": "123:abc", "VTUBER_TELEGRAM_CHAT_IDS": "42"}
    procs = nuvem.commands(env)
    assert list(procs) == ["principal", "kronus", "bit", "byte"]
    assert procs["bit"][procs["bit"].index("--agent") + 1] == "bit" and "8774" in procs["bit"] and "127.0.0.1" in procs["bit"]
    envs = nuvem.child_envs(env, procs)
    assert envs["principal"]["BYTE_AGENTES_SEPARADOS"] == "kronus,bit,byte"
    assert envs["byte"] == {"BYTE_TELEGRAM_TOKEN": "123:abc", "BYTE_TELEGRAM_CHAT_IDS": "42"}
    assert list(nuvem.commands({"BYTE_MODEL": "x"})) == ["principal", "kronus"]             # padrão de antes
    assert "bit" not in nuvem.child_envs({}, nuvem.commands({"BYTE_MODEL": "x"}))


def test_panel_tokens_cover_every_agent(tmp_path):
    (tmp_path / "painel-tokens.json").write_text('{"principal": "' + "p" * 30 + '", "kronus": "' + "k" * 30 + '"}')
    tokens = nuvem.panel_tokens(tmp_path)
    assert tokens["principal"] == "p" * 30 and tokens["kronus"] == "k" * 30                 # os antigos continuam valendo
    assert len(tokens["bit"]) >= 24 and len(tokens["byte"]) >= 24 and nuvem.panel_tokens(tmp_path) == tokens


def test_matrix_gets_the_panel_links_of_agents_running_apart(tmp_path):
    """v13.40: a Matrix abre o painel do Bit (antes ele só aparecia nos logs)."""
    import json as _json
    env = {"BYTE_MODEL": "x", "BYTE_PAINEL_REMOTO": "1", "BYTE_AGENTES": "kronus,bit", "BYTE_KRONUS_PORT": "0",
           "CONTAINER_APP_NAME": "byte", "CONTAINER_APP_ENV_DNS_SUFFIX": "x.azurecontainerapps.io"}
    sup = nuvem.Supervisor(tmp_path / "home", tmp_path / "store", tmp_path, env, interval_s=3600)
    procs = nuvem.commands(env)
    for name, extra in nuvem.child_envs(env, procs).items():
        sup.child_env.setdefault(name, {}).update(extra)
    sup.open_panels(procs)
    try:
        links = _json.loads(sup.child_env["principal"]["BYTE_PAINEIS"])
        tokens = nuvem.panel_tokens(tmp_path / "store")
        assert links == {"kronus": f"https://byte.x.azurecontainerapps.io/ui?token={tokens['kronus']}",
                         "bit": f"https://byte.x.azurecontainerapps.io/ui?token={tokens['bit']}"}
        assert sup.child_env["bit"]["BYTE_UI_TOKEN"] == tokens["bit"]
    finally:
        sup.router.stop()


def test_update_result_goes_back_to_the_agent_that_asked(tmp_path, monkeypatch):
    """v13.41: o Bit pede /atualizar -> o supervisor grava o resultado -> o BIT responde no bot dele."""
    from byte_agent import engine, headless
    home, store = tmp_path / "home", tmp_path / "store"
    make_home(home).close()
    store.mkdir()
    env = {"BYTE_DADOS_PERSISTENTES": str(store), "BYTE_GITHUB_REPO": "DragonClaw121/byte-agent"}
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(engine, "ACTIVE_AGENT", {"id": "bit", "nome": "Bit"})
    db = engine.UnifiedDatabase(tmp_path / "bit.db", engine.VectorEngine())
    svc = type("S", (), {"db": db, "extra_actions": {}})()
    headless.install_update_action(svc)
    out = svc.extra_actions["request_update"]({})
    assert "respondo aqui" in out["text"] and json.loads((store / nuvem.UPDATE_FLAG).read_text())["agent"] == "bit"
    monkeypatch.setattr(nuvem, "commands", lambda env: {"principal": [sys.executable, "-c", "import time; time.sleep(60)"]})

    class Stub:
        last_error = None
        def check_and_install(self):
            return None
    sup = nuvem.Supervisor(home, store, tmp_path, {}, interval_s=3600, updater=Stub(), update_interval_s=0,
                           restart=lambda: None)
    monkeypatch.setattr(sup, "_pause", lambda seconds: time.sleep(min(seconds, 0.2)))
    threading.Timer(2.0, sup.stopping.set).start()
    sup.run()
    assert nuvem.read_update_result(store)["status"] == "sem_novidade"
    for _ in range(40):
        msgs = [m["message"] for m in db.pop_undelivered_proactive_messages("telegram")]
        if msgs:
            break
        time.sleep(0.2)
    assert msgs == [f"✅ Já estou na versão mais nova (v{engine.BYTE_VERSION})."]
    # depois de reiniciar numa versão nova: o Bit avisa porque foi ele que pediu; o Kronus (não pediu) fica quieto
    (store / nuvem.UPDATE_RESULT).write_text(json.dumps({"agent": "bit", "status": "atualizando", "done_at": time.time()}))
    db.set_setting("versao_anunciada", "13.0")
    assert headless.announce_version(svc) is True
    monkeypatch.setattr(engine, "ACTIVE_AGENT", {"id": "kronus", "nome": "Kronus"})
    db2 = engine.UnifiedDatabase(tmp_path / "kronus.db", engine.VectorEngine())
    db2.set_setting("versao_anunciada", "13.0")
    assert headless.announce_version(type("S", (), {"db": db2})()) is False

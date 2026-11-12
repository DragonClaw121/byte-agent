"""Testes funcionais da Byte v10.1 com Ollama falso, nvidia-smi falso e páginas ollama.com simuladas."""
import http.server, importlib.util, json, os, socketserver, stat, sys, tempfile, threading, time, urllib.parse
from pathlib import Path

SRC = sys.argv[1]
tmp = Path(tempfile.mkdtemp())
GIB = 1024 ** 3

# ---------- nvidia-smi falso (Tesla T4, 15360 MiB, 14000 livres) ----------
smi_state = tmp / "smi.txt"
smi_state.write_text("Tesla T4, 15360, 1360, 14000\n")
smi = tmp / "bin" / "nvidia-smi"
smi.parent.mkdir()
smi.write_text(f"#!/bin/sh\ncat {smi_state}\n")
smi.chmod(smi.stat().st_mode | stat.S_IEXEC)
os.environ["PATH"] = f"{smi.parent}:{os.environ['PATH']}"

# ---------- Ollama falso ----------
STATE = {"installed": {"qwen2.5:3b": int(1.9 * GIB)}, "loaded": {}, "pulls": [], "chat_models": [], "chat_ctx": [],
         "sizes": {"nemotron-3.5-lightning": int(3.0 * GIB), "qwen2.5-coder:14b": int(9.0 * GIB),
                   "llama3.3:70b": int(43 * GIB), "deepseek-r1:14b": int(9.0 * GIB)}}


class Fake(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def _json(self, obj, code=200):
        b = json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

    def do_GET(self):
        if self.path == "/api/tags":
            self._json({"models": [{"name": n, "size": s} for n, s in STATE["installed"].items()]})
        elif self.path == "/api/ps":
            self._json({"models": [{"name": n, "size": s, "size_vram": (0 if STATE.get("cpu_only") else s)} for n, s in STATE["loaded"].items()]})
        else:
            self._json({}, 404)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        if self.path == "/api/pull":
            name = body["name"]; STATE["pulls"].append(name)
            if name not in STATE["sizes"]:
                self.send_response(200); self.end_headers()
                self.wfile.write(json.dumps({"error": "pull model manifest: file does not exist"}).encode() + b"\n"); return
            total = STATE["sizes"][name]
            self.send_response(200); self.end_headers()
            try:
                self.wfile.write(b'{"status":"pulling manifest"}\n')
                for frac in (0.0, 0.5, 1.0):
                    self.wfile.write(json.dumps({"status": "pulling abc", "digest": "sha256:abc", "total": total, "completed": int(total * frac)}).encode() + b"\n")
                    self.wfile.flush(); time.sleep(0.05)
                STATE["installed"][name] = total
                self.wfile.write(b'{"status":"success"}\n')
            except Exception:
                pass
        elif self.path == "/api/chat":
            STATE["chat_models"].append(body["model"]); STATE["chat_ctx"].append(body["options"]["num_ctx"])
            if body["model"] not in STATE["installed"]:
                self._json({"error": "model not found"}, 404); return
            STATE["loaded"] = {body["model"]: STATE["installed"][body["model"]]}
            if body.get("stream"):
                self.send_response(200); self.end_headers()
                STATE.setdefault("chat_bodies", []).append(body)
                replies = STATE.get("replies") or []
                content = replies.pop(0) if replies else f"resposta de {body['model']}"
                self.wfile.write(json.dumps({"message": {"content": content}, "done": False}).encode() + b"\n")
                self.wfile.write(json.dumps({"message": {"content": ""}, "done": True, "done_reason": STATE.get("done_reason", "stop"), "eval_count": 300, "eval_duration": 2_000_000_000}).encode() + b"\n")
            else:
                self._json({"message": {"content": '{"tasks": [], "memories": []}'}})
        elif self.path == "/api/generate":
            if body.get("keep_alive") == 0:
                STATE["loaded"].pop(body.get("model"), None); self._json({})
            elif body.get("model") == "quebrado:1b":
                self._json({"error": "llama runner process has terminated"}, 500)
            else:
                time.sleep(STATE.get("load_delay", 0)); STATE.setdefault("preloads", []).append((body["model"], body["options"]["num_ctx"]))
                STATE["loaded"] = {body["model"]: STATE["installed"][body["model"]]}; self._json({"done": True})
        else:
            self._json({}, 404)


class TS(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True; daemon_threads = True


srv = TS(("127.0.0.1", 0), Fake)
threading.Thread(target=srv.serve_forever, daemon=True).start()
OLLAMA = f"http://127.0.0.1:{srv.server_address[1]}"

spec = importlib.util.spec_from_file_location("byte", SRC)
byte = importlib.util.module_from_spec(spec); sys.modules["byte"] = byte; spec.loader.exec_module(byte)
R = byte.TrueLLMAutonomousRouter

ok_count = 0
def check(cond, label):
    global ok_count
    assert cond, "FALHOU: " + label
    ok_count += 1
    print("  ok:", label)

# ============ 1. normalização ============
print("[1] normalização sem alias")
for raw, want in [("nemotron-3.5-lightning", "nemotron-3.5-lightning"), ("nemotron-3.5-super", "nemotron-3.5-super"),
                  ("nemotron", "nemotron"), ("deepseek-r1:14b", "deepseek-r1:14b"), ("qwen2.5-coder:14b", "qwen2.5-coder:14b"),
                  ("llama3.3:70b", "llama3.3:70b"), ("  ollama:qwen2.5-coder : 14b ", "qwen2.5-coder:14b"),
                  ("ollama run llama3.3:70b", "llama3.3:70b"), ("nemotron 3.5 super", "nemotron-3.5-super"),
                  ("deepseek", "deepseek"), ("mistral", "mistral"), ("hf.co/user/Repo-GGUF:Q4_K_M", "hf.co/user/Repo-GGUF:Q4_K_M"),
                  ("qwen2.5:7b-instruct-q4_K_M", "qwen2.5:7b-instruct-q4_K_M")]:
    got, note = R.normalize_ollama_model_name(raw)
    check(got == want and R.is_valid_ollama_tag(got), f"{raw!r} -> {got!r}")
check(not hasattr(R, "HUMAN_MODEL_ALIASES"), "HUMAN_MODEL_ALIASES removido")
for bad in ["modelo;rm -rf", "a b$c", "../etc", "x:y:z", ""]:
    got, _ = R.normalize_ollama_model_name(bad)
    check(not R.is_valid_ollama_tag(got), f"inválida rejeitada: {bad!r}")

# ============ instância ============
vec = byte.VectorEngine()
db = byte.UnifiedDatabase(tmp / "t.db", vec, drive_backup_path=tmp / "bk.db")
llm = R(db=db, ollama_url=OLLAMA)
llm._ollama_is_local = lambda: True
llm.mydrive_path = tmp / "nodrive"
check(llm.ollama_online and llm.ollama_installed == ["qwen2.5:3b"], "Ollama falso online")

# ============ 2. hardware ============
print("[2] telemetria e validador")
hw = llm.inspect_gpu_hardware(force=True)
check(hw["gpu_name"] == "Tesla T4" and hw["vram_total_gb"] == 15.0 and abs(hw["vram_free_gb"] - 13.67) < 0.01, f"nvidia-smi lido: {hw['gpu_name']} {hw['vram_free_gb']}/{hw['vram_total_gb']}")
check(hw["ram_total_gb"] > 0 and hw["hardware_known"], f"RAM lida: {hw['ram_free_gb']}/{hw['ram_total_gb']} GB")
ram_budget = round(hw["ram_free_gb"] * 0.7, 2)
v = llm.evaluate_model_vram_safety(9.0)
check(v["level"] == "safe" and v["label"] == "🟢 SEGURO (100% GPU VRAM)", "9 GB na T4 = seguro")
v = llm.evaluate_model_vram_safety(12.47)
check(v["level"] == "safe", "limite: 12.47+1.2 <= 13.67")
v = llm.evaluate_model_vram_safety(12.6)
check(v["level"] == ("hybrid" if ram_budget >= 0.2 else "oom"), f"12.6 GB excede a VRAM -> {v['level']}")
big = 13.67 + ram_budget + 5
v = llm.evaluate_model_vram_safety(big)
check(v["level"] == "oom" and not v["allowed"] and abs(v["missing_gb"] - 6.2) < 0.05 and "Tesla T4" in v["message"], f"OOM: faltam {v['missing_gb']} GB, GPU citada")
llm.force_oom_override = True
check(llm.evaluate_model_vram_safety(big)["allowed"] is True, "force_oom_override libera")
llm.force_oom_override = False
# VRAM recuperável: modelo carregado conta como disponível
smi_state.write_text("Tesla T4, 15360, 11360, 4000\n")
STATE["loaded"] = {"qwen2.5:3b": int(8 * GIB)}
hw = llm.inspect_gpu_hardware(force=True)
v = llm.evaluate_model_vram_safety(9.0)
check(hw["ollama_vram_gb"] == 8.0 and v["reclaimable_vram_gb"] == round(4000 / 1024 + 8.0, 2) and v["level"] == "safe", f"VRAM recuperável = livre + Ollama ({v['reclaimable_vram_gb']} GB)")
STATE["loaded"] = {}
smi_state.write_text("Tesla T4, 15360, 1360, 14000\n")
llm.inspect_gpu_hardware(force=True)
check(R.estimate_vram_gb_from_label("14b") == 9.1 and R.estimate_vram_gb_from_label("7b") == 4.7 and R.estimate_vram_gb_from_label("e4b") == 2.9
      and R.estimate_vram_gb_from_label("335m") == 0.6 and R.estimate_vram_gb_from_label("lightning") is None
      and R.estimate_vram_gb_from_label("7b-instruct-q4_k_m") == 4.7, "estimativa Q4_K_M por parâmetros")

# ============ 3. busca ao vivo (HTML simulado) ============
print("[3] busca ao vivo")
SEARCH_HTML = '''<ul>
<li x-test-model class="x"><a href="/library/qwen2.5-coder" class="g"><h2><span x-test-search-response-title>qwen2.5-coder</span></h2>
<p class="max-w-lg">The latest series of Code-Specific Qwen models &amp; more.</p>
<span x-test-capability>tools</span><span x-test-size class="a">0.5b</span><span x-test-size>1.5b</span><span x-test-size>7b</span><span x-test-size>14b</span><span x-test-size>32b</span></a></li>
<li x-test-model class="x"><a href="/library/nemotron-3.5-lightning"><p class="x">NVIDIA Nemotron 3.5 lightning.</p><span x-test-size>lightning</span><span x-test-size>cloud</span></a></li>
<li x-test-model><a href="/someuser/qwen2.5-coder-abliterated"><p class="x">Community build of the coder model.</p><span x-test-size>7b</span></a></li>
</ul><a href="/blog/x">blog</a>'''
TAGS_HTML = '''<div><a href="/library/qwen2.5-coder:latest" class="group">qwen2.5-coder:latest</a><p>dae161e27b0e • 4.7GB • 32K context window</p></div>
<div><a href="/library/qwen2.5-coder:14b">qwen2.5-coder:14b</a><span>9.0GB</span></div><div><a href="/library/qwen2.5-coder:14b">dup</a></div>
<div><a href="/library/qwen2.5-coder:0.5b">x</a><span>398MB</span></div><div><a href="/library/qwen2.5-coder:32b">x</a> 20GB </div>'''
calls = []
def fake_get(url, timeout_s=8.0, accept="text/html", max_bytes=1_500_000):
    calls.append(url)
    if "/search?" in url: return SEARCH_HTML
    if url.endswith("/library/qwen2.5-coder/tags"): return TAGS_HTML
    if "/v2/library/nemotron-3.5-lightning/manifests/latest" in url:
        return json.dumps({"config": {"size": 500}, "layers": [{"size": int(3.0 * GIB)}, {"size": 1000}]})
    if "/v2/library/modelo-inexistente/manifests/latest" in url:
        raise byte.urllib.error.HTTPError(url, 404, "nf", None, None)
    raise TimeoutError("página fora do ar")
llm._http_get_text = fake_get
live = llm.search_ollama_library_online("qwen2.5-coder")
check(live["ok"] and live["names"] == ["qwen2.5-coder", "someuser/qwen2.5-coder-abliterated"], f"slugs: {live['names']}")
check(live["variants"]["qwen2.5-coder"] == ["0.5b", "1.5b", "7b", "14b", "32b"], "variantes x-test-size")
check(live["descriptions"]["qwen2.5-coder"].startswith("The latest series of Code-Specific Qwen models & more"), "descrição extraída")
tg = llm.fetch_ollama_library_tags("qwen2.5-coder")
sizes = {t["tag"]: t["size_gb"] for t in tg["tags"]}
check(sizes == {"latest": 4.7, "14b": 9.0, "0.5b": 0.39, "32b": 20.0}, f"página de tags: {sizes}")
reg = llm.inspect_registry_tag("qwen2.5-coder:14b")
check(reg["exists"] is True and reg["size_gb"] == 9.0 and reg["source"] == "ollama.com/tags", "tag confirmada na página de tags")
reg = llm.inspect_registry_tag("nemotron-3.5-lightning")
check(reg["exists"] is True and reg["size_gb"] == 3.0 and reg["source"] == "manifesto do registro", "fallback imediato para o manifesto da tag direta")
reg = llm.inspect_registry_tag("modelo-inexistente")
check(reg["exists"] is False, "tag inexistente detectada (404)")

res = llm.search_discoverable_models("qwen2.5-coder:14b")
by = {m["name"]: m for m in res["models"]}
d = by["qwen2.5-coder:14b"]
check(res["models"][0]["name"] == "qwen2.5-coder:14b" and d["vram_gb"] == 9.0 and d["safety"]["level"] == "safe" and d["registry_exists"] is True, "tag digitada: 1º item, 9.0 GB reais, 🟢")
res = llm.search_discoverable_models("qwen2.5-coder")
by = {m["name"]: m for m in res["models"]}
for n in ["qwen2.5-coder", "qwen2.5-coder:0.5b", "qwen2.5-coder:1.5b", "qwen2.5-coder:14b", "qwen2.5-coder:32b", "someuser/qwen2.5-coder-abliterated:7b"]:
    check(n in by, f"variante acionável listada: {n}")
check(by["qwen2.5-coder:32b"]["vram_gb"] == 20.0 and by["qwen2.5-coder:32b"]["safety"]["level"] in ("hybrid", "oom"), f"32b = 20 GB reais -> {by['qwen2.5-coder:32b']['safety']['label']}")
check(by["qwen2.5-coder:1.5b"]["size_source"].startswith("estimado") and by["qwen2.5-coder:1.5b"]["vram_gb"] == 1.3, "1.5b estimado por parâmetros")
check(res["gpu_hardware"]["gpu_name"] == "Tesla T4" and res["online_search"]["count"] >= 6, "hardware + contagem online na resposta")
check(all(m.get("safety") for m in res["models"] if m["provider"] == "ollama"), "todo modelo Ollama tem veredito")
res = llm.search_discoverable_models("nemotron-3.5-lightning")
d = res["models"][0]
check(d["name"] == "nemotron-3.5-lightning" and d["vram_gb"] == 3.0 and not d["installed"], "nemotron-3.5-lightning preservado, 3.0 GB do manifesto")
check(not any("cloud" in m["name"] for m in res["models"]), "variantes cloud descartadas")
# site fora do ar: a tag digitada continua acionável
llm._http_get_text = lambda *a, **k: (_ for _ in ()).throw(TimeoutError("offline"))
llm._online_search_cache.clear(); llm._registry_cache.clear()
res = llm.search_discoverable_models("deepseek-r1:14b")
check(res["models"][0]["name"] == "deepseek-r1:14b" and res["models"][0]["vram_gb"] == 9.1 and res["online_search"]["ok"] is False, "sem rede: tag direta com estimativa")

# ============ 1/3. aplicar modelo não instalado => download real ============
print("[4] download, warmup e ativação")
def wait_idle(t=15):
    end = time.time() + t
    while time.time() < end and llm.provision_state["running"]: time.sleep(0.05)
    assert not llm.provision_state["running"], "provisionador não terminou"

st = llm.configure(provider="ollama", model_name="nemotron-3.5-lightning", pull_model_async=True)
check(st["download_started"] and st["active_model"] == "nemotron-3.5-lightning", "Aplicar Modelo: download iniciado, sem alias")
wait_idle()
check(STATE["pulls"] == ["nemotron-3.5-lightning"], f"/api/pull recebeu a tag exata: {STATE['pulls']}")
check(llm.active_model == "nemotron-3.5-lightning" and "nemotron-3.5-lightning" in STATE["loaded"], "aquecido e ativo como modelo principal")
check(llm.pull_progress["activated"] and llm.pull_progress["pct"] == 100 and llm.pull_progress["safety"]["level"] == "safe" and "100% GPU" in (llm.pull_progress["gpu_alloc"] or ""), f"progresso final: {llm.pull_progress['gpu_alloc']}")

r = llm.pull_and_activate_model("ollama:qwen2.5-coder:14b")
check(r["model"] == "qwen2.5-coder:14b", "Baixar & Ativar aceita prefixo ollama:")
wait_idle()
check(llm.active_model == "qwen2.5-coder:14b" and STATE["pulls"][-1] == "qwen2.5-coder:14b", "qwen2.5-coder:14b baixado e ativo")

# OOM bloqueado dinamicamente (70b = 43 GB)
ram_free = llm.inspect_gpu_hardware(force=True)["ram_free_gb"]
expect_block = 43 + 1.2 > 15.0 + ram_free * 0.7
llm.pull_and_activate_model("llama3.3:70b")
wait_idle()
if expect_block:
    check("llama3.3:70b" not in STATE["installed"] and "RISCO DE OOM" in (llm.pull_progress["error"] or "") and "Tesla T4" in llm.pull_progress["error"] and "faltam" in llm.pull_progress["error"], "70b bloqueado: " + llm.pull_progress["error"][:150])
    check(llm.active_model == "qwen2.5-coder:14b", "modelo ativo preservado após bloqueio")
    llm.pull_and_activate_model("llama3.3:70b", force_oom_override=True)
    wait_idle()
    check("llama3.3:70b" in STATE["installed"] and llm.active_model == "llama3.3:70b", "force_oom_override=True permite baixar e ativar")
    llm.force_oom_override = False
else:
    print("  (máquina de teste com RAM suficiente para híbrido; bloqueio OOM coberto pelo teste unitário)")

# tag inexistente: erro explícito, sem troca silenciosa
llm.pull_and_activate_model("modelo-que-nao-existe:1b")
wait_idle()
check("file does not exist" in (llm.provision_state.get("error") or "") and llm.pull_progress["error"], "tag inexistente: erro explícito do registro")
try:
    llm.configure(provider="ollama", model_name="tag inválida$$"); check(False, "deveria falhar")
except ValueError as e:
    check("inválida" in str(e), "configure rejeita tag inválida com erro")

# ============ generate: pedido não instalado dispara download ============
print("[5] generate / extrator / num_ctx")
llm.configure(provider="ollama", model_name="qwen2.5:3b", pull_model_async=True); wait_idle()
llm.auto_effort_routing = False
llm.active_model = "deepseek-r1:14b"           # simula modelo ativo ainda não instalado
n_pulls = len(STATE["pulls"])
out = llm.generate("oi", [], [], [], "")
check(out["model_fallback"] and out["model_fallback"]["requested"] == "ollama:deepseek-r1:14b" and "download" in out["model_fallback"]["reason"], "fallback explícito com status do download: " + out["model_fallback"]["reason"])
wait_idle()
check(STATE["pulls"][n_pulls:] == ["deepseek-r1:14b"] and llm.active_model == "deepseek-r1:14b" and "deepseek-r1:14b" in STATE["loaded"], "download automático concluído e modelo ativado")
out = llm.generate("oi de novo", [], [], [], "")
check(out["model_fallback"] is None and "deepseek-r1:14b" in out["provider_used"], "turno seguinte já usa o modelo pedido")

# 4.2 num_ctx estável no auto-routing
llm.auto_effort_routing = True
STATE["chat_ctx"].clear()
llm.generate("Analise passo a passo a complexidade deste algoritmo de arquitetura completo e detalhado, compare e otimize", [], [], [], "")
llm.generate("oi", [], [], [], "")
check(set(STATE["chat_ctx"]) == {llm.num_ctx}, f"num_ctx constante no auto-routing: {STATE['chat_ctx']}")
llm.generate("oi", [], [], [], "", num_ctx_input=4096)
check(STATE["chat_ctx"][-1] == 4096, "num_ctx_input respeitado no auto-routing")

# 4.1 extrator usa o modelo quente
STATE["loaded"] = {"qwen2.5-coder:14b": STATE["installed"]["qwen2.5-coder:14b"]}
llm.probe_online_resources(force=True)
llm.active_model = "qwen2.5:3b"
STATE["chat_models"].clear()
byte.ContextGatekeeper.has_potential_commitment_or_memory = staticmethod(lambda m: True)
llm.extract_agenda_and_memories_via_llm("Tenho reunião amanhã às 10h")
check(STATE["chat_models"] == ["qwen2.5-coder:14b"], f"extrator usou o modelo quente: {STATE['chat_models']}")

# arena não troca competidor
ar = llm._run_single_arena_contestant("ollama", "phi4:14b", "teste", [])
check(ar["done_reason"] == "not_installed" and "não está instalado" in ar["response"], "arena: sem substituição de competidor")

# 4.4 cópias rasas
db.add_memory_direct("Gabriel usa Arch Linux com Hyprland", importance=5)
hcr = byte.HybridContextReader(vec) if "ve" in byte.HybridContextReader.__init__.__code__.co_varnames else byte.HybridContextReader(vec)
hits = hcr.top_memories(db, "arch linux hyprland")
check(hits and all("_hcr" not in m for m in db._get_cached_memories()), "cache de memórias não recebe '_hcr'")

# 4.3 URLs puras
src_txt = Path(SRC).read_text(encoding="utf-8")
check('"https" + "://[' not in src_txt and src_txt.count("](https://") == 1, "sem literais de URL em formato Markdown (resta só a amostra de teste do sanitize_url)")
check(byte.sanitize_url("https://api.openai.com/v1/chat/completions") == "https://api.openai.com/v1/chat/completions", "sanitize_url preserva URL pura")

# v10.1.1: autocorreção quando o servidor carrega tudo na RAM
print("[6] autocorreção de GPU")
repairs = []
def fake_repair(t0, force_reinstall=False):
    repairs.append(force_reinstall); STATE["loaded"] = {}
    if fix_at is not None and len(repairs) >= fix_at: STATE["cpu_only"] = False
    return True
llm._repair_ollama_gpu = fake_repair
# caso do print do usuário: servidor antigo só-CPU com o modelo JÁ carregado (atalho "VRAM já aquecida")
llm.configure(provider="ollama", model_name="qwen2.5:3b", pull_model_async=True); wait_idle()
STATE["cpu_only"] = True; fix_at = 1; llm._gpu_repair_stage = 0
llm.probe_online_resources(force=True); llm.inspect_gpu_hardware(force=True)
check(llm.get_status()["loaded_on_cpu_only"] == ["qwen2.5:3b"], "status acusa modelo na RAM com GPU ociosa")
llm.start_online_setup_async(target_model="qwen2.5:3b", force_restart=True); wait_idle()
steps = " ".join(llm.provision_state["steps"])
check(repairs == [False] and "não está usando a GPU" in steps and "100% GPU" in steps and llm.get_status()["loaded_on_cpu_only"] == [], "modelo já carregado na RAM => restart do servidor + warmup real em 100% GPU")
# restart simples não resolve => 2ª etapa reinstala
STATE["cpu_only"] = True; fix_at = 2; repairs.clear(); llm._gpu_repair_stage = 0
llm.probe_online_resources(force=True); llm.inspect_gpu_hardware(force=True)
llm.start_online_setup_async(target_model="qwen2.5:3b", force_restart=True); wait_idle()
check(repairs == [False, True] and llm.get_status()["loaded_on_cpu_only"] == [], "escalonamento: restart, depois reinstalação forçada")
# nada resolve => aviso explícito, sem laço
STATE["cpu_only"] = True; fix_at = None; repairs.clear(); llm._gpu_repair_stage = 0
llm.probe_online_resources(force=True); llm.inspect_gpu_hardware(force=True)
llm.start_online_setup_async(target_model="qwen2.5:3b", force_restart=True); wait_idle()
check(repairs == [False, True] and "continua 100% em CPU/RAM" in " ".join(llm.provision_state["steps"]), "reparo sem efeito: 2 tentativas, aviso explícito, sem laço")
llm.start_online_setup_async(target_model="qwen2.5:3b", force_restart=True); wait_idle()
check(len(repairs) == 2, "sem novas tentativas automáticas depois de esgotar")
fix_at = 1; repairs.clear()
r = llm.repair_gpu_async(); wait_idle()
check(repairs == [True] and "ollama_version" in r["diagnostics_before"] and llm.get_status()["loaded_on_cpu_only"] == [], "botão Reparar GPU força reinstalação e reaquece")
STATE["cpu_only"] = False
os.environ["CUDA_VISIBLE_DEVICES"] = "-1"; check("CUDA_VISIBLE_DEVICES" not in llm._ollama_gpu_env(), "CUDA_VISIBLE_DEVICES=-1 removido"); del os.environ["CUDA_VISIBLE_DEVICES"]
check(R._ollama_cuda_libs_present(None) is False, "runtime CUDA ausente detectado")
d = llm.ollama_gpu_diagnostics(); check(isinstance(d["log_gpu_lines"], list) and "server_processes" in d, "diagnóstico de GPU disponível")

# v10.1.3: arena com troca de modelo
print("[7] arena")
STATE["load_delay"] = 1.5; STATE["installed"]["quebrado:1b"] = GIB
llm.probe_online_resources(force=True)
res = llm.compare_models_arena("teste", "ollama:qwen2.5:3b", "ollama:qwen2.5-coder:14b", [])
a, b = res["contestant_a"], res["contestant_b"]
check(not a["failed"] and not b["failed"] and "resposta de qwen2.5-coder:14b" in b["response"], "os dois competidores respondem após a troca de modelo")
check(b["load_ms"] >= 1400 and b["latency_ms"] < 1000, f"carga ({b['load_ms']} ms) fora do cronômetro ({b['latency_ms']} ms)")
check(STATE["preloads"][-1] == ("qwen2.5-coder:14b", llm.num_ctx) and STATE["chat_ctx"][-1] == llm.num_ctx, "pré-carga e chat usam o mesmo num_ctx (sem recarga)")
check(llm._cold_load_timeout_s("qwen2.5-coder:14b") == 207.0 and llm._cold_load_timeout_s("qwen2.5:3b") >= 60, "limite de carga proporcional ao tamanho (9 GB -> 207 s)")
STATE["load_delay"] = 0
res = llm.compare_models_arena("teste", "ollama:qwen2.5:3b", "ollama:quebrado:1b", [])
b = res["contestant_b"]
check(b["failed"] and "está instalado, mas não respondeu" in b["response"] and "load_error" in b["response"] and "o outro competidor falhou" in res["summary"]["faster_model"], "falha real relatada (não 'não instalado') e sem vencedor falso")

# v10.2
print("[8] raciocínio, idioma, autonomia, memórias, arena")
check(byte.strip_reasoning_markup("<think>Okay, the user wants X</think>\nOlá, Gabriel!") == "Olá, Gabriel!", "bloco <think> removido")
check(byte.strip_reasoning_markup("planning...</think>Resposta final") == "Resposta final" and byte.strip_reasoning_markup("Oi <think>cortado") == "Oi", "fechamento órfão e bloco aberto removidos")
EN = "Okay, the user is asking about what you would do with this and that. Here is how you can have it."
PT = "Claro, Gabriel! Você pode começar pela revisão do diagrama de classes, que é a parte mais importante para o projeto."
check(byte.looks_english(EN) and not byte.looks_english(PT) and not byte.looks_english("```python\nfor the in and: is are you\n```\nPronto, o código está acima e não tem erro."), "detector de inglês")
llm.configure(provider="ollama", model_name="qwen2.5:3b", pull_model_async=True); wait_idle(); llm.auto_effort_routing = False
STATE["replies"] = ["<think>the user says hi</think>" + EN, PT]; STATE["chat_bodies"] = []
out = llm.generate("oi, tudo bem?", [], [], [], "")
check(out["response"] == PT and out["language_retry"] and "<think>" not in out["response"], "resposta em inglês => reescrita em português, sem raciocínio")
check("português do Brasil" in STATE["chat_bodies"][0]["messages"][0]["content"][-600:], "regra de idioma no fim do system prompt")
STATE["replies"] = [EN]
out = llm.generate("please answer in english: what is a flip-flop?", [], [], [], "")
check(not out["language_retry"], "inglês pedido explicitamente não é reescrito")
# autonomia
ctx = byte.SlidingWindowContextManager(db, "teste")
eng = byte.ProactiveAutonomyEngine(db, llm, ctx)
check(eng.idle_threshold_sec == 600 and eng.min_interval_sec == 1800 and eng.max_unanswered == 1, "ritmo padrão: 10 min ocioso, 30 min entre iniciativas")
STATE["replies"] = [PT]
r1 = eng.trigger_proactive_thought("vram_ready_autonomy")
r2 = eng.trigger_proactive_thought("autonomous_idle_check")
check(r1["triggered"] and not r2["triggered"], "2ª iniciativa bloqueada enquanto a 1ª está sem resposta: " + r2["reason"])
eng.notify_user_activity()
r3 = eng.trigger_proactive_thought("autonomous_idle_check")
check(not r3["triggered"] and "Intervalo" in r3["reason"], "intervalo mínimo respeitado mesmo após atividade")
eng2 = byte.ProactiveAutonomyEngine(db, llm, ctx)
check(not eng2.trigger_proactive_thought("vram_ready_autonomy")["triggered"], "reiniciar a Byte não gera nova saudação dentro do intervalo")
db.set_setting("last_proactive_epoch", 0); STATE["replies"] = [PT]
check(not eng2.trigger_proactive_thought("autonomous_idle_check")["triggered"], "mensagem repetida descartada")
db.set_setting("last_proactive_epoch", 0); db.set_setting("last_proactive_text", ""); STATE["replies"] = [EN, EN]
r = eng2.trigger_proactive_thought("autonomous_idle_check")
check(not r["triggered"] and "inglês" in r["reason"], "iniciativa em inglês descartada após 1 tentativa de reescrita")
STATE["replies"] = ["Gabriel, que tal revisarmos agora a agenda de amanhã para não esquecer nada importante?"]
check(eng2.trigger_proactive_thought("user_requested_initiative")["triggered"], "botão manual continua funcionando")
# memórias
n_before = len(db.list_memories()); r = db.clear_all_memories()
check(n_before > 0 and r["removed"] == n_before and db.list_memories() == [] and db._get_cached_memories() == [], f"limpar memórias: {r['removed']} apagada(s), cache zerado")
# arena
STATE["chat_bodies"] = []; STATE["replies"] = []
res = llm.compare_models_arena("teste", "ollama:qwen2.5:3b", "ollama:qwen2.5-coder:14b", [])
a = res["contestant_a"]
check(STATE["chat_bodies"][0]["options"]["num_predict"] == 4096 and a["token_budget"] == 4096, "arena: orçamento padrão de 4096 tokens")
check(a["tokens_generated"] == 300 and a["tokens_per_sec"] == 150.0 and a["metric_source"].startswith("real"), "arena: tok/s real (eval_count ÷ eval_duration)")
STATE["done_reason"] = "length"
res = llm.compare_models_arena("teste", "ollama:qwen2.5:3b", "ollama:qwen2.5-coder:14b", [], max_tokens=512)
check(res["contestant_a"]["truncated"] and len(res["summary"]["truncated"]) == 2 and STATE["chat_bodies"][-1]["options"]["num_predict"] == 512, "arena: resposta cortada é sinalizada")
STATE["done_reason"] = "stop"

# status
stt = llm.get_status()
check(stt["gpu_hardware"]["gpu_name"] == "Tesla T4" and "active_model_safety" in stt and "force_oom_override" in stt, "get_status expõe GPU e veredito")
html_ui = byte.build_v90_web_ui({"llm": stt, "version": byte.BYTE_VERSION}, token="abc")
(tmp / "ui.html").write_text(html_ui, encoding="utf-8")
print(f"\nTODOS OS {ok_count} TESTES PASSARAM | UI em {tmp / 'ui.html'}")

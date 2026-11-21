"""v13.33 (R0.4): memória com embeddings da Azure (a nuvem não tem Ollama; antes a busca era só por palavras)."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from byte_agent import cloud, engine

TOPICS = {"cafe": 0, "coado": 0, "bebida": 0, "quente": 0, "cachorro": 1, "pet": 1, "animal": 1, "rex": 1}


def fake_embed(texts):
    out = []
    for t in texts:
        v = [0.0] * 8
        for w in engine.VectorEngine.tokenize(t):
            if w in TOPICS:
                v[TOPICS[w]] += 1.0
        v[7] += 0.05
        out.append(v)
    return out


def make_db(tmp_path):
    ve = engine.VectorEngine()
    return engine.UnifiedDatabase(tmp_path / "m.db", ve), ve


def test_meaning_search_and_reindex(tmp_path):
    db, ve = make_db(tmp_path)
    db.add_memory_direct("Meu café favorito é coado e sem açúcar", importance=4)
    db.add_memory_direct("Tenho um cachorro chamado Rex", importance=4)
    # modo local (palavras): "bebida quente" não acha o café
    assert not any("café" in m["content"] for m in db.search_memories_hybrid("qual bebida quente eu gosto?"))
    ve.remote_embedder, ve.remote_name = fake_embed, "byte-embed"
    assert db.reindex_memories() == {"done": 2, "left": 0}
    with db.connection() as conn:
        assert {r["embedding_engine"] for r in conn.execute("SELECT embedding_engine FROM memories")} == {"neural:azure-byte-embed"}
    hits = db.search_memories_hybrid("qual bebida quente eu gosto?")
    assert hits and "café" in hits[0]["content"]
    assert "Rex" in db.search_memories_hybrid("e o meu pet?")[0]["content"]
    assert db.reindex_memories() == {"done": 0, "left": 0}


def test_falls_back_when_azure_is_down(tmp_path):
    db, ve = make_db(tmp_path)

    def broken(texts):
        raise cloud.CloudLLMError("fora do ar")
    ve.remote_embedder, ve.remote_name = broken, "byte-embed"
    db.add_memory_direct("Prefiro reuniões de manhã", importance=4)       # grava com o modo local
    with db.connection() as conn:
        assert conn.execute("SELECT embedding_engine FROM memories").fetchone()[0] == "hash-v1"
    assert db.search_memories_hybrid("reuniões de manhã")                   # a busca segue funcionando
    assert db.reindex_memories()["done"] == 0                               # fica para depois


@pytest.fixture()
def azure():
    calls = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append({"path": self.path, "body": body, "key": self.headers.get("api-key")})
            if "dimensions" in body and len(calls) == 1:
                out, code = {"error": {"message": "This model does not support 'dimensions'."}}, 400
            else:
                out, code = {"data": [{"index": i, "embedding": [0.1] * 1536} for i, _ in enumerate(body["input"])],
                             "usage": {"total_tokens": 7}}, 200
            raw = json.dumps(out).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", calls
    httpd.shutdown()


def test_azure_client(azure):
    url, calls = azure
    read = lambda names: {"BYTE_AZURE_EMBED": "byte-embed", "AZURE_OPENAI_ENDPOINT": url,
                          "AZURE_OPENAI_API_KEY": "k"}.get(names[0], "")
    emb = cloud.AzureEmbeddings.from_env(read)
    vecs = emb.embed(["a", "b"])
    assert len(vecs) == 2 and len(vecs[0]) == 1536 and not emb.send_dims          # modelo sem "dimensions": desliga
    assert calls[-1]["path"].startswith("/openai/deployments/byte-embed/embeddings?") and calls[-1]["key"] == "k"
    assert cloud.AzureEmbeddings.from_env(lambda names: "") is None
    ve = engine.VectorEngine()
    ve.remote_embedder, ve.remote_name = emb.embed, "byte-embed"
    v, eng = ve.embed_with_engine("teste")
    assert eng == "neural:azure-byte-embed" and len(v) == 128

"""本地模型服务 —— 没有 SiliconFlow key 时跑真实召回链路用。

- POST /v1/embeddings  : OpenAI 兼容({"model","input"} → {"data":[{"embedding"}]})
- POST /v1/rerank      : SiliconFlow 兼容({"query","documents","top_n"} →
                          {"results":[{"index","relevance_score"}]}),分数经 sigmoid 落到 0~1,
                          与线上 bge-reranker 的两极分化分布同形。

与线上模型不同(线上: SiliconFlow embedding + bge-reranker-v2-m3),所以只用于同一环境内的
A/B 对照,绝对指标不可与 reports/final 直接比较。

  .venv-eval/bin/python tests/eval/jev_gate/local_models_server.py  (默认 :18080)
"""

from __future__ import annotations

import json
import math
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from fastembed import TextEmbedding
from fastembed.rerank.cross_encoder import TextCrossEncoder

EMBED_MODEL = os.environ.get("LOCAL_EMBED_MODEL", "jinaai/jina-embeddings-v2-base-zh")
RERANK_MODEL = os.environ.get("LOCAL_RERANK_MODEL", "BAAI/bge-reranker-base")
PORT = int(os.environ.get("LOCAL_MODELS_PORT", "18080"))
# 同机多开实例并行评测时限定每个实例的 ONNX 线程数,否则互相抢 CPU、请求超时,评测结果被精排失败污染。
# 不设 = ONNX 默认(用满)。实测(10 核 = 4 性能核 + 6 能效核):3 个不限线程的实例 → 大面积超时;
# 2 个 × 5 线程最快;2 个 × 8 线程反而慢约 30%(线程排到能效核上,每步都要等最慢的线程)。
THREADS = int(os.environ["LOCAL_MODELS_THREADS"]) if os.environ.get("LOCAL_MODELS_THREADS") else None

_embedder = TextEmbedding(EMBED_MODEL, threads=THREADS)
_reranker = TextCrossEncoder(RERANK_MODEL, threads=THREADS)
_lock = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, obj: dict) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        try:
            req = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            if self.path.rstrip("/").endswith("/embeddings"):
                texts = req["input"] if isinstance(req["input"], list) else [req["input"]]
                with _lock:
                    vecs = [v.tolist() for v in _embedder.embed(texts)]
                self._send(200, {"data": [{"index": i, "embedding": v} for i, v in enumerate(vecs)]})
            elif self.path.rstrip("/").endswith("/rerank"):
                docs = req["documents"]
                with _lock:
                    logits = list(_reranker.rerank(req["query"], docs))
                scored = sorted(((i, 1 / (1 + math.exp(-float(s)))) for i, s in enumerate(logits)),
                                key=lambda x: x[1], reverse=True)
                top_n = req.get("top_n") or len(docs)
                self._send(200, {"results": [{"index": i, "relevance_score": s} for i, s in scored[:top_n]]})
            else:
                self._send(404, {"error": "not found"})
        except Exception as e:  # 让调用方按 5xx 处理
            self._send(500, {"error": str(e)})

    def log_message(self, *args) -> None:
        pass


if __name__ == "__main__":
    print(f"local models on :{PORT} embed={EMBED_MODEL} rerank={RERANK_MODEL}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()

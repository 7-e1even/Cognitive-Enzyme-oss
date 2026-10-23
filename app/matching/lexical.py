"""词面召回(BM25)—— 与语义向量互补的"表层线索"识别。

向量召回擅长"意思相近",但具体情境与抽象记忆之间常有语义鸿沟;两者却往往共享罕见的
字面线索:CVE 描述里的 "SQL injection" 对 CWE-89、判决里的法律术语、缺陷报告里的组件名。
真实基准(tests/eval/realbench)离线测算:向量前 50 条之外再并入 BM25 前 10 条,正确记忆的
覆盖率 65.9% → 74.1%,法律 42.7% → 59.3%、安全 32% → 48%;不调 LLM,几乎不加延迟。

实现:Qdrant 原生稀疏向量,单独的 `<collection>_lexical` 索引(与经验样本同样的派生数据,
PG 为准)。IDF 由 Qdrant 按库统计(modifier=idf),客户端只算词频饱和项。分词:英文/数字词
(去停用词)+ 中文连续汉字的二元组,中英文混合可用,不依赖外部分词器。

默认开启(运行参数 recall.lexical,环境变量 CE_LEXICAL_RECALL=off 也可关闭);索引无论开关都会维护(本地计算,零 API 成本)。
端到端实跑(2026-09-25,新选择阈值):本地评测台 F1 57.8 → 58.6;SiliconFlow 评测台(bge-m3 +
bge-reranker-v2-m3)真实基准 F1 60.4 → 61.1("没召回" 195 → 170),建议类评测集 Recall 91% → 95%、
Precision 不变。
"""

from __future__ import annotations

import re
import zlib
from collections import Counter

from app import settings

K1, B = 1.2, 0.75

_STOP = frozenset(
    "the a an of to in and or for on with is are was were be been by as at from that this it its into not no "
    "can could should would will may might have has had do does did but if then than so such which who whom "
    "what when where how why all any each other more most some only own same very i you he she we they them his "
    "her our your their there here also about after before over under between during while because these those "
    "的 了 是 在 和 与 或 及 等".split())
_WORD = re.compile(r"[a-z0-9_]+(?:[.\-][a-z0-9_]+)*")
_HAN_RUN = re.compile(r"[一-鿿]+")
_QUERY_CHARS = 6000


def lexical_on() -> bool:
    return settings.get("recall.lexical")


def lexical_limit() -> int:
    return settings.get("recall.lexical_limit")


def _avgdl() -> float:
    return settings.get("recall.lexical_avgdl")


def tokenize(text: str) -> list[str]:
    t = (text or "").lower()
    toks = []
    for w in _WORD.findall(t):
        toks.append(w)
        if "-" in w:                    # use-after-free / cross-site:整词 + 拆开的部件都算
            toks += w.split("-")
    toks = [w for w in toks if len(w) > 1 and w not in _STOP]
    for run in _HAN_RUN.findall(t):
        toks += [run[i:i + 2] for i in range(len(run) - 1)] if len(run) > 1 else [run]
    return [w for w in toks if w not in _STOP]


def _term_id(tok: str) -> int:
    return zlib.crc32(tok.encode("utf-8")) & 0x7FFFFFFF


def doc_vector(text: str) -> tuple[list[int], list[float]] | None:
    """记忆文本 → (term ids, BM25 词频饱和权重)。IDF 由 Qdrant 在检索时乘上。"""
    toks = tokenize(text)
    if not toks:
        return None
    tf = Counter(_term_id(t) for t in toks)
    norm = K1 * (1 - B + B * len(toks) / _avgdl())
    ids = sorted(tf)
    return ids, [tf[i] * (K1 + 1) / (tf[i] + norm) for i in ids]


def query_vector(text: str) -> tuple[list[int], list[float]] | None:
    """情境 → 去重后的 term ids(权重 1,打分 = Σ IDF × 词频饱和项,即 BM25)。"""
    ids = sorted({_term_id(t) for t in tokenize((text or "")[:_QUERY_CHARS])})
    return (ids, [1.0] * len(ids)) if ids else None


def merge_lexical(hits: list[dict], lex_hits: list[dict]) -> list[dict]:
    """把 BM25 命中并入向量召回。两路分数尺度不同(余弦 vs BM25),按名次对齐:
    BM25 第 r 名记为向量第 r 名的分数,再按酶取最高——两路前几名交错排在最前面,
    后续与经验样本命中(同为余弦)的合并也保持可比。"""
    if not lex_hits:
        return hits
    scale = [float(h.get("_score", 0.0)) for h in hits]
    best: dict[str, dict] = {h["id"]: h for h in hits if h.get("id")}
    for r, h in enumerate(lex_hits):
        eid = h.get("id")
        if not eid:
            continue
        s = scale[r] if r < len(scale) else (scale[-1] if scale else 0.0)
        if eid not in best or s > float(best[eid].get("_score", 0.0)):
            best[eid] = {**best.get(eid, {}), "id": eid, "_score": s}
    return sorted(best.values(), key=lambda h: -float(h.get("_score", 0.0)))

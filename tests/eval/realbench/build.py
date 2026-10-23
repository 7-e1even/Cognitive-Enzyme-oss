"""真实数据多领域基准(realbench)构建 —— 记忆、情境、标注全部来自公开真实数据,不含任何生成内容。

重点是"高级模型自己不知道、需要外挂或提醒"的知识:

| 层 | 领域 | 记忆 | 情境(查询) | 标注 |
|---|---|---|---|---|
| 外部知识 | firefox | Mozilla Bugzilla 2025– 的已知问题(现象 + 状态 + 首条描述) | 之后用户提交的重复报告 | 维护者标 DUPLICATE 的 dupe_of |
| 外部知识 | legal | LePaRD:联邦判决里被引用的判例段落原文 | 后续判决中的引用语境(已抹掉判例引文) | 真实引用关系 |
| 外部知识 | biomed | SciFact 5183 篇论文摘要 | 专家基于引文写的科学论断 | 专家标注 |
| 提醒 | security | MITRE CWE 官方弱点条目 | NVD 2024–2025 真实 CVE 描述 | NVD 标注;CWE ChildOf 父子为可接受 |
| 提醒 | theorem | Wikipedia 定理条目摘要(TheoremQA 自带说明为 GPT-4 生成,弃用) | TheoremQA 教材/考试原题 | 专家标注 |
| 静默 | — | — | DailyDialog 日常对话开场句 | 不应触发任何记忆 |

另产出按时间排序的真实经验流(firefox / legal),用于测"越用越会认"的自我改进。
数据许可各异,产物不入库:
  .venv-eval/bin/python -m tests.eval.realbench.build
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
CACHE = Path.home() / ".cache" / "ce-realbench"
UA = {"User-Agent": "cognitive-enzyme-realbench/1.0 (research eval; contact: repo maintainer)"}

CWE_ZIP = "https://cwe.mitre.org/data/xml/cwec_latest.xml.zip"
TQA_JSON = "https://raw.githubusercontent.com/wenhuchen/TheoremQA/main/theoremqa_test.json"
HF_PARQUET = "https://huggingface.co/api/datasets/{ds}/parquet/{cfg}/{split}"
BZ = "https://bugzilla.mozilla.org/rest"


# ── 下载与缓存 ─────────────────────────────────────────────────

def fetch(url: str, name: str | None = None) -> Path:
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / (name or ("url_" + hashlib.sha1(url.encode()).hexdigest()))
    if not path.exists():
        for attempt in range(5):
            try:
                req = urllib.request.Request(url, headers=UA)
                with urllib.request.urlopen(req, timeout=120) as resp:
                    data = resp.read()
                tmp = path.with_suffix(".part")
                tmp.write_bytes(data)
                tmp.replace(path)
                break
            except Exception:
                if attempt == 4:
                    raise
                time.sleep(3 * (attempt + 1))
    return path


def fetch_json(url: str) -> dict:
    return json.loads(fetch(url).read_text(encoding="utf-8"))


def hf_rows(ds: str, cfg: str, split: str, shards: int | None = None) -> list[dict]:
    import pyarrow.parquet as pq
    urls = json.loads(fetch(HF_PARQUET.format(ds=ds, cfg=cfg, split=split),
                            f"{ds.replace('/', '__')}__{cfg}__{split}.urls.json").read_text())
    rows: list[dict] = []
    for i, url in enumerate(urls[:shards] if shards else urls):
        rows += pq.read_table(fetch(url, f"{ds.replace('/', '__')}__{cfg}__{split}__{i}.parquet")).to_pylist()
    return rows


def _clean(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + "…"


def _mem(id_, patterns, then, because, domain, tags, severity="warning"):
    return {"id": id_, "patterns": [{"value": p} for p in patterns if p], "then": then,
            "because": because, "domain": domain, "severity": severity, "confidence": 0.5,
            "tags": tags, "origin": "harvested"}


def _q(id_, domain, text, expected, source, acceptable=(), ts=""):
    return {"id": id_, "kind": "positive", "domain": domain, "text": text, "expected_ids": list(expected),
            "acceptable_ids": list(acceptable), "source": source, "ts": ts}


# ── firefox:Mozilla Bugzilla 已知问题 × 重复报告 ────────────────

_BUGREF = re.compile(r"(?i)(\bbug\s*#?\s*\d{5,8}\b|show_bug\.cgi\?id=\d+|bugzil\.la/\d+|\b\d{7}\b)")


def _bz_page(params: dict) -> list[dict]:
    return fetch_json(f"{BZ}/bug?{urllib.parse.urlencode(params, doseq=True)}").get("bugs", [])


def _bz_all(params: dict, page: int = 500, max_pages: int = 100) -> list[dict]:
    out, offset = [], 0
    for _ in range(max_pages):
        bugs = _bz_page({**params, "limit": page, "offset": offset})
        out += bugs
        if len(bugs) < page:
            break
        offset += page
    return out


def _bz_first_comment(bug_id: int) -> str:
    try:
        d = fetch_json(f"{BZ}/bug/{bug_id}/comment")
        comments = d["bugs"][str(bug_id)]["comments"]
        return comments[0]["text"] if comments else ""
    except Exception:
        return ""


def build_firefox(rng: random.Random, n_static: int, n_stream: int, n_distract: int):
    fields = "id,summary,dupe_of,creation_time,product,component,status,resolution"
    dups = []
    for prod in ("Firefox", "Core"):
        dups += _bz_all({"product": prod, "resolution": "DUPLICATE", "creation_time": "2025-01-01",
                         "include_fields": fields})
    info: dict[int, dict] = {b["id"]: b for b in dups}

    def load(ids):
        todo = sorted({i for i in ids if i and i not in info})
        for k in range(0, len(todo), 200):
            for b in _bz_page({"id": todo[k:k + 200], "include_fields": fields}):
                info[b["id"]] = b

    def root(bid):  # 顺着 dupe_of 链找到最终的原始问题
        seen = set()
        while bid in info and info[bid].get("dupe_of") and bid not in seen:
            seen.add(bid)
            nxt = info[bid]["dupe_of"]
            load([nxt])
            if nxt not in info:
                return None
            bid = nxt
        return bid

    load(b["dupe_of"] for b in dups)
    pairs = []
    for b in dups:
        r = root(b["id"])
        if r and r != b["id"] and info[r].get("resolution") != "DUPLICATE":
            pairs.append((b, r))
    masters = sorted({r for _, r in pairs})
    # 干扰项:同期已修复的缺陷(只取前几页——Bugzilla 深分页极慢,够用即可)
    distract = [b for b in _bz_all({"product": ["Firefox", "Core"], "resolution": "FIXED",
                                    "creation_time": "2025-01-01", "include_fields": fields},
                                   page=500, max_pages=5)
                if b["id"] not in set(masters)]
    distract = rng.sample(distract, min(n_distract, len(distract)))

    pairs.sort(key=lambda x: x[0]["creation_time"])
    by_master = defaultdict(list)
    for b, r in pairs:
        by_master[r].append(b)
    static_pool = [b for lst in by_master.values() for b in lst[:2]]
    static = rng.sample(static_pool, min(n_static, len(static_pool)))
    static_ids = {b["id"] for b in static}
    stream = [(b, r) for b, r in pairs if b["id"] not in static_ids][:n_stream]
    need_comments = set(masters) | {b["id"] for b in distract} | static_ids | {b["id"] for b, _ in stream}
    with ThreadPoolExecutor(6) as ex:
        comments = dict(zip(sorted(need_comments), ex.map(_bz_first_comment, sorted(need_comments))))

    def mem(b):
        status = f"{b.get('status', '')} {b.get('resolution', '')}".strip()
        return _mem(f"bz-{b['id']}", [b["summary"]],
                    f"Known {b['product']}::{b['component']} issue (Mozilla bug {b['id']}, {status}): {b['summary']}",
                    _clean(comments.get(b["id"], ""), 500), "firefox",
                    ["source:bugzilla.mozilla.org", f"component:{b['component']}"])

    root_of = {b["id"]: r for b, r in pairs}

    def query_text(b):
        text = f"{b['summary']}\n{comments.get(b['id'], '')}"
        return _clean(_BUGREF.sub("[bug ref]", text), 900)   # 防泄漏:抹掉 bug 编号引用

    memories = [mem(info[m]) for m in masters] + [mem(b) for b in distract]
    queries = [_q(f"bz-q{b['id']}", "firefox", query_text(b), [f"bz-{root_of[b['id']]}"],
                  f"bugzilla:{b['id']}", ts=b["creation_time"]) for b in static]
    stream_q = [_q(f"bz-s{b['id']}", "firefox", query_text(b), [f"bz-{r}"], f"bugzilla:{b['id']}",
                   ts=b["creation_time"]) for b, r in stream]
    return memories, queries, stream_q, {"duplicates": len(dups), "pairs": len(pairs), "masters": len(masters)}


# ── legal:LePaRD 判例段落 × 引用语境 ──────────────────────────

_CITE = re.compile(r"[A-Z][\w.&'’ -]{0,80}\bv\.\s[\w.&'’ ,-]{0,100}?\d+\s(?:U\.\s?S\.|F\.\s?(?:2d|3d|4th)?|F\.\s?Supp\.(?:\s?[23]d)?|S\.\s?Ct\.|L\.\s?Ed\.)[^)]{0,60}\)?")


def build_legal(rng: random.Random, n_static: int, n_stream: int, n_memories: int):
    rows = hf_rows("rmahari/LePaRD", "default", "train", shards=1)
    freq = Counter(r["passage_id"] for r in rows)
    popular = [pid for pid, c in freq.most_common() if c >= 4][: n_memories // 2]
    rest = [pid for pid, c in freq.items() if c < 4]
    chosen = set(popular) | set(rng.sample(rest, min(n_memories - len(popular), len(rest))))
    first = {}
    for r in rows:
        if r["passage_id"] in chosen and r["passage_id"] not in first:
            first[r["passage_id"]] = r
    memories = [_mem(f"lep-{pid}", [_clean(r["quote"], 60)], _clean(r["quote"], 700),
                     f"{r['source_court']} ({r['source_date']})", "legal",
                     ["source:LePaRD", f"court:{r['source_court']}"], severity="info")
                for pid, r in first.items()]

    def ctx(r):
        text = _CITE.sub("[citation]", r["destination_context"] or "")
        return _clean(text[-900:], 900)

    cands = [r for r in rows if r["passage_id"] in chosen and len(ctx(r)) >= 200]
    cands.sort(key=lambda r: r["dest_date"] or "")
    by_pid = defaultdict(list)
    for r in cands:
        by_pid[r["passage_id"]].append(r)
    static = rng.sample([lst[0] for lst in by_pid.values()], min(n_static, len(by_pid)))
    static_keys = {(r["dest_id"], r["passage_id"]) for r in static}
    stream = [r for r in cands if r["passage_id"] in set(popular)
              and (r["dest_id"], r["passage_id"]) not in static_keys][:n_stream]

    def q(r, prefix):
        return _q(f"{prefix}{r['dest_id']}-{r['passage_id']}", "legal", ctx(r), [f"lep-{r['passage_id']}"],
                  f"LePaRD:{r['dest_cite']}", ts=r["dest_date"] or "")
    return memories, [q(r, "lep-q") for r in static], [q(r, "lep-s") for r in stream], \
        {"rows": len(rows), "popular_passages": len(popular)}


# ── biomed:SciFact ────────────────────────────────────────────

def build_biomed(rng: random.Random, n_static: int):
    corpus = hf_rows("BeIR/scifact", "corpus", "corpus")
    claims = {str(r["_id"]): r["text"] for r in hf_rows("BeIR/scifact", "queries", "queries")}
    qrels = defaultdict(list)
    for r in hf_rows("BeIR/scifact-qrels", "default", "test"):
        qrels[str(r["query-id"])].append(f"sf-{r['corpus-id']}")
    memories = [_mem(f"sf-{r['_id']}", [_clean(r["title"], 120)],
                     _clean(f"{r['title']} {r['text']}", 800), "", "biomed", ["source:SciFact"], severity="info")
                for r in corpus]
    ids = {m["id"] for m in memories}
    usable = [qid for qid, docs in qrels.items() if qid in claims and all(d in ids for d in docs)]
    queries = [_q(f"sf-q{qid}", "biomed", claims[qid], qrels[qid], f"SciFact:{qid}")
               for qid in rng.sample(usable, min(n_static, len(usable)))]
    return memories, queries


# ── security:CWE × CVE(提醒类)───────────────────────────────

def build_security(rng: random.Random, n_static: int):
    ns = {"c": "http://cwe.mitre.org/cwe-7"}
    with zipfile.ZipFile(fetch(CWE_ZIP, "cwec_latest.xml.zip")) as zf:
        root = ET.fromstring(zf.read(next(n for n in zf.namelist() if n.endswith(".xml"))))
    version = root.attrib.get("Version", "")

    def text_of(el):
        return " ".join(t.strip() for t in el.itertext()) if el is not None else ""

    memories, parents = [], defaultdict(set)
    for w in root.iterfind(".//c:Weakness", ns):
        if w.attrib.get("Status") in ("Deprecated", "Obsolete"):
            continue
        cid = f"cwe-{w.attrib['ID']}"
        mit = w.find("c:Potential_Mitigations/c:Mitigation/c:Description", ns)
        alt = [text_of(t) for t in w.iterfind("c:Alternate_Terms/c:Alternate_Term/c:Term", ns)][:3]
        for rel in w.iterfind("c:Related_Weaknesses/c:Related_Weakness", ns):
            if rel.attrib.get("Nature") == "ChildOf" and rel.attrib.get("View_ID") == "1000":
                parents[cid].add(f"cwe-{rel.attrib['CWE_ID']}")
        memories.append(_mem(cid, [w.attrib.get("Name", "")] + alt, _clean(text_of(w.find("c:Description", ns)), 500),
                             _clean("Mitigation: " + text_of(mit), 300) if mit is not None else "",
                             "security", [f"source:cwe-{version}", f"abstraction:{w.attrib.get('Abstraction', '')}"]))
    ids = {m["id"] for m in memories}
    children = defaultdict(set)
    for c, ps in parents.items():
        for p in ps:
            children[p].add(c)
    by_cwe = defaultdict(list)
    for r in hf_rows("stasvinokur/cve-and-cwe-dataset-1999-2025", "default", "train"):
        cve, cwe = r.get("CVE-ID") or "", (r.get("CWE-ID") or "").strip().lower()
        if re.match(r"CVE-202[45]-", cve) and re.fullmatch(r"cwe-\d+", cwe) and cwe in ids and r.get("DESCRIPTION"):
            by_cwe[cwe].append((cve, _clean(r["DESCRIPTION"], 1200)))
    pool = [x + (c,) for c, lst in by_cwe.items() for x in rng.sample(lst, min(4, len(lst)))]
    queries = [_q(f"sec-{cve}", "security", text, [cwe], f"nvd:{cve}", sorted((parents[cwe] | children[cwe]) & ids))
               for cve, text, cwe in rng.sample(pool, min(n_static, len(pool)))]
    return memories, queries


# ── theorem:TheoremQA × Wikipedia(提醒类)─────────────────────

_GENERIC = {"theorem", "theorems", "law", "laws", "formula", "rule", "the", "of", "and", "s", "equation",
            "principle", "method", "model", "s'"}


def _wiki(name: str) -> tuple[str, str] | None:
    search = fetch_json("https://en.wikipedia.org/w/api.php?action=query&list=search&srlimit=1&format=json&srsearch="
                        + urllib.parse.quote(name))
    hits = search.get("query", {}).get("search", [])
    if not hits:
        return None
    title = hits[0]["title"]
    need = [t for t in re.findall(r"[a-z0-9']+", name.lower()) if t not in _GENERIC]
    toks = re.findall(r"[a-z0-9']+", title.lower())
    if not need or not all(any(tt[:5] == t[:5] for tt in toks) for t in need):
        return None
    summ = fetch_json("https://en.wikipedia.org/api/rest_v1/page/summary/" + urllib.parse.quote(title.replace(" ", "_")))
    extract = summ.get("extract") or ""
    return (title, extract) if len(extract) > 80 else None


def build_theorem(rng: random.Random, n_static: int):
    qs = [q for q in json.loads(fetch(TQA_JSON, "theoremqa_test.json").read_text()) if not q.get("Picture")]
    rng.shuffle(qs)
    names = sorted({q["theorem"].strip().lower() for q in qs})
    with ThreadPoolExecutor(4) as ex:
        hits = dict(zip(names, ex.map(lambda n: _safe(_wiki, n), names)))
    resolved = {n: "thm-" + re.sub(r"[^a-z0-9]+", "-", n).strip("-") for n, h in hits.items() if h}
    memories = [_mem(resolved[n], [n, hits[n][0]], _clean(hits[n][1], 600), "", "theorem",
                     [f"source:wikipedia:{hits[n][0]}", "labels:TheoremQA"], severity="info") for n in resolved]
    usable = [q for q in qs if q["theorem"].strip().lower() in resolved][:n_static]
    queries = [_q(f"tqa-{i:03d}", "theorem", _clean(q["Question"], 1500), [resolved[q["theorem"].strip().lower()]],
                  f"TheoremQA:{q['id']}") for i, q in enumerate(usable)]
    return memories, queries, {"theorems": len(names), "resolved": len(resolved)}


def _safe(fn, *a):
    try:
        return fn(*a)
    except Exception:
        return None


# ── silent:DailyDialog ────────────────────────────────────────

def build_silent(rng: random.Random, n: int):
    firsts = [r["utterances"][0].strip() for r in hf_rows("roskoN/dailydialog", "full", "test")
              if r.get("utterances") and len(r["utterances"][0].strip()) >= 30]
    return [{"id": f"silent-{i:03d}", "kind": "silent", "domain": "", "text": _clean(t, 500),
             "expected_ids": [], "acceptable_ids": [], "source": "DailyDialog:test", "ts": ""}
            for i, t in enumerate(rng.sample(firsts, min(n, len(firsts))))]


DOMAINS = ("firefox", "legal", "biomed", "security", "theorem", "silent")


def _write(name: str, memories: list | None, queries: list, stream: list | None = None, stat=None) -> None:
    (DATA / "corpus").mkdir(parents=True, exist_ok=True)
    (DATA / "parts").mkdir(parents=True, exist_ok=True)
    if memories is not None:
        (DATA / "corpus" / f"{name}.json").write_text(json.dumps(memories, ensure_ascii=False, indent=1),
                                                     encoding="utf-8")
    (DATA / "parts" / f"queries_{name}.json").write_text(json.dumps(queries, ensure_ascii=False, indent=1),
                                                        encoding="utf-8")
    if stream is not None:
        (DATA / f"stream_{name}.json").write_text(json.dumps({"queries": stream}, ensure_ascii=False, indent=1),
                                                  encoding="utf-8")
    if stat:
        (DATA / "parts" / f"stats_{name}.json").write_text(json.dumps(stat, ensure_ascii=False, indent=1),
                                                          encoding="utf-8")


def merge() -> dict:
    """把已构建的各领域查询合并成 queries.json(只合并语料已就绪的领域)。"""
    queries, built = [], []
    for name in DOMAINS:
        part = DATA / "parts" / f"queries_{name}.json"
        if part.exists() and (name == "silent" or (DATA / "corpus" / f"{name}.json").exists()):
            queries += json.loads(part.read_text(encoding="utf-8"))
            built.append(name)
    (DATA / "queries.json").write_text(json.dumps({"queries": queries}, ensure_ascii=False, indent=1),
                                       encoding="utf-8")
    mems = {f.stem: len(json.loads(f.read_text(encoding="utf-8"))) for f in (DATA / "corpus").glob("*.json")}
    return {"built": built, "memories": mems, "queries": dict(Counter(q["domain"] or "silent" for q in queries))}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--domains", default=",".join(DOMAINS), help="逗号分隔,可单独构建某几个领域")
    ap.add_argument("--external", type=int, default=150, help="外部知识每领域静态查询数")
    ap.add_argument("--reminder", type=int, default=100, help="提醒类每领域静态查询数")
    ap.add_argument("--silent", type=int, default=60)
    ap.add_argument("--stream", type=int, default=800)
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()
    todo = [d.strip() for d in args.domains.split(",") if d.strip()]
    for name in todo:
        rng = random.Random(f"{args.seed}:{name}")   # 每个领域独立随机源:单独构建与整体构建结果一致
        if name == "firefox":
            m, q, st, stat = build_firefox(rng, args.external, args.stream, n_distract=1500)
            _write(name, m, q, st, stat)
        elif name == "legal":
            m, q, st, stat = build_legal(rng, args.external, args.stream, n_memories=2000)
            _write(name, m, q, st, stat)
        elif name == "biomed":
            m, q = build_biomed(rng, args.external)
            _write(name, m, q)
        elif name == "security":
            m, q = build_security(rng, args.reminder)
            _write(name, m, q)
        elif name == "theorem":
            m, q, stat = build_theorem(rng, args.reminder)
            _write(name, m, q, stat=stat)
        elif name == "silent":
            _write(name, None, build_silent(rng, args.silent))
        print(f"[built] {name}", flush=True)
    print(json.dumps(merge(), ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()

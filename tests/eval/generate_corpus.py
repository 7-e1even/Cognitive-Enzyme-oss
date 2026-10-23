# tests/eval/generate_corpus.py
"""LLM 生成多领域评测语料与 golden 查询。

用法:
  python -m tests.eval.generate_corpus            # 生成酶语料 → corpus/*.json
  python -m tests.eval.generate_corpus --queries  # 生成查询 → queries.json
幂等:已存在的领域文件跳过(想重生成先删文件)。
"""
from __future__ import annotations

import asyncio
import json
import random
import sys
from pathlib import Path

from app.config import LLMConfig
from app.llm_client import chat_json

EVAL_DIR = Path(__file__).parent
CORPUS_DIR = EVAL_DIR / "corpus"
QUERIES_PATH = EVAL_DIR / "queries.json"

# deviation: 生产 runtime 配置 max_tokens=1024 装不下 10 条/批的 JSON(会被截断成非法
# JSON),且推理模型(deepseek)单批生成远超 chat() 默认 30s 超时。main() 里基于
# eval_stores() 拿到的 LLMConfig 放大 max_tokens 后赋给 LLM_CFG,所有调用显式传入。
LLM_CFG: LLMConfig | None = None
LLM_TIMEOUT = 300.0

RNG = random.Random(20260706)  # 固定种子,confidence 分布可复现

DOMAINS: dict[str, str] = {
    "math": "数学(数值稳定性、组合与概率陷阱、代数与证明技巧、估算)",
    "physics": "物理(量纲分析、守恒律、数量级估算、对称性直觉)",
    "logic": "逻辑推理(常见谬误识别、反例构造、量词与蕴含、归纳的边界)",
    "methodology": "方法论(调试方法、实验设计、奥卡姆剃刀、决策与权衡框架)",
    "software": "软件开发(并发、缓存一致性、API 设计惯例、性能直觉)",
}
BATCHES_PER_DOMAIN = 5
PER_BATCH = 10
LANG_MODES = ["中文", "English", "中英混合"]

SYSTEM_ENZYME = """你在为一个"认知酶"记忆系统生成评测语料。一条酶 = 一条专家直觉:
在某种情境模式出现时,专家脑中会立刻弹出的提醒/经验/反例。

输出 JSON 数组,每条:
{
  "patterns": [{"value": "触发关键词"}, ...],   // 2~4 个,是情境里会出现的词,不是学科名
  "then": "专家直觉建议,一句话,≤80字,可执行",
  "because": "为什么,≤60字",
  "severity": "critical|warning|info",          // 大多数 info/warning,严重陷阱才 critical
  "tags": ["自由标签"],
  "conditions": [],                             // 见下
  "cluster": ""                                 // 见下
}

要求:
1. 全部条目属于领域「{domain_desc}」,但 patterns 必须是具体情境词(如"阶乘 溢出"),
   不要放学科名本身。
2. 其中必须包含 2 组"易混淆近邻簇":同簇 2~3 条语义相近但适用条件不同
   (如"浮点比较用容差" vs "十进制金额用定点数")。同簇条目 cluster 填同一个短标签,
   并且**每条**在 conditions 里放 1 个能区分它与同簇兄弟的情境关键词
   (该词出现在上下文中时这条才适用)。非簇条目 cluster 为 "" 且 conditions 为 []。
3. 本批语言:{lang}。patterns/then/because 都用这个语言写。
4. 条目之间不重复,且要覆盖领域内不同子话题。"""


async def gen_corpus() -> None:
    CORPUS_DIR.mkdir(exist_ok=True)
    for domain, desc in DOMAINS.items():
        out = CORPUS_DIR / f"{domain}.json"
        if out.exists():
            print(f"skip {domain}(已存在)")
            continue
        enzymes: list[dict] = []
        for b in range(BATCHES_PER_DOMAIN):
            lang = LANG_MODES[b % len(LANG_MODES)]
            system = SYSTEM_ENZYME.replace("{domain_desc}", desc).replace("{lang}", lang)
            user = f"生成第 {b+1}/{BATCHES_PER_DOMAIN} 批,共 {PER_BATCH} 条。已有条目的 then 摘要:\n" + \
                   "\n".join(f"- {e['then'][:40]}" for e in enzymes[-20:])
            batch = await chat_json(system, user, LLM_CFG, timeout=LLM_TIMEOUT)
            if not isinstance(batch, list):
                print(f"  batch {b} 返回非法,重试一次")
                batch = await chat_json(system, user, LLM_CFG, timeout=LLM_TIMEOUT)
            if not isinstance(batch, list):
                raise RuntimeError(f"{domain} batch {b} 生成失败")
            for raw in batch:
                # deviation: skip entries with empty patterns/then — they'd fail
                # validate.py's schema checks later, so filter here instead of
                # writing broken corpus entries to disk.
                # deviation: LLM 偶尔把 patterns 写成裸字符串数组,归一化成 {"value": ...};
                # conditions 偶尔写成 {"value": ...} 字典或 "a|b|c" 管道串,归一化成 str 列表
                patterns = [p if isinstance(p, dict) else {"value": str(p)}
                            for p in raw.get("patterns", [])]
                conditions = []
                for c in raw.get("conditions", []):
                    c = c.get("value", "") if isinstance(c, dict) else str(c)
                    conditions.extend(part.strip() for part in c.split("|") if part.strip())
                then = raw.get("then", "")
                if not patterns or not then:
                    print(f"  skip 空 patterns/then 条目: {raw}")
                    continue
                idx = len(enzymes)
                enzymes.append({
                    "id": f"{domain}-{idx:03d}",
                    "patterns": patterns,
                    "conditions": conditions,
                    "then": then,
                    "because": raw.get("because", ""),
                    "severity": raw.get("severity", "info"),
                    "tags": raw.get("tags", []) + (
                        [f"cluster:{raw['cluster']}"] if raw.get("cluster") else []),
                    "domain": domain,
                    "origin": "harvested",
                    "confidence": round(RNG.uniform(0.3, 0.9), 2),
                })
            print(f"{domain}: {len(enzymes)} 条")
        out.write_text(json.dumps(enzymes, ensure_ascii=False, indent=2),
                       encoding="utf-8")


SYSTEM_QUERY = """你在为记忆召回系统生成评测查询。给你一条"认知酶"(专家直觉),
写一个真实用户 prompt,该 prompt 出现时这条直觉**应该**被召回。

要求:
- 不要抄酶原文的措辞,用情境侧的自然表达(可含代码片段、报错、任务描述)。
- 风格 {style}。语言 {lang}。
- 输出 JSON: {"text": "..."}"""

QUERY_STYLES = ["一段较长的任务上下文(80~150字)", "一个简短的问题(≤30字)",
                "夹一小段代码或报错的求助"]

SILENT_TOPICS = [
    "周末去哪里徒步", "红烧肉的做法", "推荐几部悬疑电影", "猫咪总在半夜叫怎么办",
    "欧洲杯赛程", "如何给盆栽换土", "附近有什么好吃的火锅", "健身增肌饮食安排",
    "学吉他先练什么", "旅行保险怎么选",
]


async def gen_queries() -> None:
    from tests.eval.validate import load_corpus
    enzymes = load_corpus(CORPUS_DIR)
    RNG.shuffle(enzymes)
    per_domain: dict[str, int] = {}
    queries: list[dict] = []
    for e in enzymes:
        if per_domain.get(e.domain, 0) >= 20:
            continue
        i = len(queries)
        style = QUERY_STYLES[i % len(QUERY_STYLES)]
        lang = LANG_MODES[i % len(LANG_MODES)]
        system = SYSTEM_QUERY.replace("{style}", style).replace("{lang}", lang)
        cond = f"\n注意:查询里必须自然地包含关键词「{e.conditions[0]}」" if e.conditions else ""
        user = f"酶: 当 {e.patterns_readable} 时 → {e.then}(因为 {e.because}){cond}"
        result = await chat_json(system, user, LLM_CFG, timeout=LLM_TIMEOUT)
        if not result or not result.get("text"):
            continue
        per_domain[e.domain] = per_domain.get(e.domain, 0) + 1
        queries.append({
            "id": f"q{len(queries):03d}", "kind": "positive", "domain": e.domain,
            "text": result["text"], "expected_ids": [e.id], "acceptable_ids": [],
        })
        print(f"q{len(queries):03d} [{e.domain}] ← {e.id}")

    for j, topic in enumerate(SILENT_TOPICS * 3):
        queries.append({
            "id": f"s{j:03d}", "kind": "silent", "domain": "",
            "text": topic + ("？" if j % 2 else "，有什么建议"),
            "expected_ids": [], "acceptable_ids": [],
        })
    QUERIES_PATH.write_text(
        json.dumps({"queries": queries}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    print(f"共 {len(queries)} 条查询")


async def main() -> None:
    # bootstrap 只为把 DB 里的 LLM key 装进全局默认(chat_json 用)
    from dataclasses import replace

    from tests.eval.bootstrap import eval_stores
    global LLM_CFG
    config, pg, _ = await eval_stores()
    await pg.close()
    LLM_CFG = replace(config.llm, max_tokens=8192)
    if "--queries" in sys.argv:
        await gen_queries()
    else:
        await gen_corpus()


if __name__ == "__main__":
    asyncio.run(main())

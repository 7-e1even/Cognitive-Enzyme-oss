# tests/eval/annotate_queries.py
"""为 positive 查询扩标 acceptable_ids:
向量近邻 top-10 中,LLM 判定"对该查询也有帮助"的酶记为 acceptable —
它们被注入时不该罚 precision。expected 保持生成时的强标注。

用法: python -m tests.eval.annotate_queries

实现相对 brief 的偏差(已在 task-7-report.md 记录):
- chat_json 传 timeout=120.0(生产 LLM 是推理模型,默认 30s 偏紧)。
- 单条查询的 <=9 个候选近邻先合并成一次 chat_json 调用(返回 JSON 判定数组),
  而不是逐条调用 —— 判定标准(宁缺毋滥)与逐条版本一致,只是省 LLM 调用次数。
  实测发现:生产 LLM(deepseek-v4-pro,max_tokens=1024)偶尔会把 JSON 数组
  pretty-print 展开,导致约一半的批量调用在 1024 token 内被截断解析失败。
  为此额外要求"单行紧凑 JSON"以降低截断概率;若批量调用仍失败(返回 None 或
  没有合法 verdicts 数组),自动逐条 fallback 到 brief 原版的单候选判定,
  保证每条查询在本次运行内就能拿到确定结果,而不是留一个空 acceptable_ids
  等下次重跑再猜。
"""
from __future__ import annotations

import asyncio
import json

from app.llm_client import chat_json, embed
from tests.eval.bootstrap import eval_stores
from tests.eval.validate import CORPUS_DIR, QUERIES_PATH, load_corpus

SYSTEM_BATCH = """判断若干条"专家直觉"是否分别对一个用户查询有帮助。
relevant=true 的标准:把这条直觉展示给正在处理该查询的人,他会觉得"有用/相关",
而不是"无关的噪声"。宁缺毋滥,拿不准就 false。
输出 JSON: {"verdicts": [{"id": "<直觉id>", "relevant": true|false}, ...]}
verdicts 必须覆盖输入里的每一条候选,id 需要和输入的 id 完全一致。
只输出单行紧凑 JSON,不要换行、不要缩进、不要多余文字。"""

SYSTEM_SINGLE = """判断一条"专家直觉"对一个用户查询是否有帮助。
输出 JSON: {"relevant": true|false}
relevant=true 的标准:把这条直觉展示给正在处理该查询的人,他会觉得"有用/相关",
而不是"无关的噪声"。宁缺毋滥。"""

TIMEOUT = 120.0


def _build_user_prompt(query_text: str, candidates: list[tuple[str, object]]) -> str:
    lines = [f"查询: {query_text}", "候选直觉:"]
    for eid, e in candidates:
        lines.append(f"- id={eid}: {e.then}(因为 {e.because})")
    return "\n".join(lines)


async def _judge_batch(
    query_text: str, candidates: list[tuple[str, object]]
) -> list[str] | None:
    """一次调用判定所有候选。返回 acceptable_ids,失败返回 None。"""
    verdict = await chat_json(
        SYSTEM_BATCH, _build_user_prompt(query_text, candidates), timeout=TIMEOUT
    )
    if not verdict or not isinstance(verdict.get("verdicts"), list):
        return None

    relevant_by_id = {
        v.get("id"): bool(v.get("relevant"))
        for v in verdict["verdicts"]
        if isinstance(v, dict) and "id" in v
    }
    # 批量结果必须覆盖每一个候选,否则视为不可信(截断/漏项),走 fallback。
    if not all(eid in relevant_by_id for eid, _ in candidates):
        return None
    return [eid for eid, _ in candidates if relevant_by_id[eid]]


async def _judge_single(query_text: str, eid: str, e: object) -> bool:
    verdict = await chat_json(
        SYSTEM_SINGLE,
        f"查询: {query_text}\n直觉: {e.then}(因为 {e.because})",
        timeout=TIMEOUT,
    )
    return bool(verdict and verdict.get("relevant"))


async def _judge_candidates(
    query_text: str, candidates: list[tuple[str, object]]
) -> tuple[list[str], bool]:
    """返回 (acceptable_ids, used_fallback)。批量成功时 used_fallback=False。"""
    if not candidates:
        return [], False

    batch_result = await _judge_batch(query_text, candidates)
    if batch_result is not None:
        return batch_result, False

    # Fallback: 逐条判定(brief 原版算法),更稳但调用数更多。
    acceptable = []
    for eid, e in candidates:
        if await _judge_single(query_text, eid, e):
            acceptable.append(eid)
    return acceptable, True


async def main() -> None:
    config, pg, qdrant = await eval_stores()
    by_id = {e.id: e for e in load_corpus(CORPUS_DIR)}
    data = json.loads(QUERIES_PATH.read_text(encoding="utf-8"))

    n_annotated = 0
    n_embed_failed = 0
    n_fallback = 0
    total_acceptable = 0

    for q in data["queries"]:
        if q["kind"] != "positive" or q["acceptable_ids"]:
            continue
        vector = await embed(q["text"], config=config.embedding)
        if not vector:
            print(f"{q['id']}: embed 失败,跳过(下次重跑会重试)")
            n_embed_failed += 1
            continue
        hits = await qdrant.search(vector, limit=10)
        candidates: list[tuple[str, object]] = []
        for h in hits:
            eid = h["id"]
            if eid in q["expected_ids"] or eid not in by_id:
                continue
            candidates.append((eid, by_id[eid]))

        acceptable, used_fallback = await _judge_candidates(q["text"], candidates)
        if used_fallback:
            n_fallback += 1

        q["acceptable_ids"] = acceptable
        n_annotated += 1
        total_acceptable += len(acceptable)
        # 每条查询后立即落盘:单轮命令可能超时被杀,增量保存让重跑真正续传
        # (代价:0 acceptable 的查询下次会重判,结果幂等,可接受)
        QUERIES_PATH.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"{q['id']}: +{len(acceptable)} acceptable"
              f"{' (fallback)' if used_fallback else ''}")

    QUERIES_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    await pg.close()

    print(
        f"\n完成: annotated={n_annotated} embed_failed={n_embed_failed} "
        f"fallback_used={n_fallback} total_acceptable={total_acceptable}"
    )


if __name__ == "__main__":
    asyncio.run(main())

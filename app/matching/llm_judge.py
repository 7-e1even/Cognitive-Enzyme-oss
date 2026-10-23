"""LLM 第二判官 —— 与 Jev 并行,对同一候选池做列表式适用性判定。

Jev 的 noul 是逐条独立判断;LLM 看到整张候选清单,能做相对比较。两者的
"是否直接有用"概率取平均(融合)再交给 Stage 4.5 的选择策略,离线对照
(tests/eval/jev_gate/README.md §4):自家集 / TheoremQA / MetaTool 上 R/P/静默
9 个数全部持平或提升,无一变差。

默认关闭(运行参数 judge.llm);用绑定的「第二判官模型」(model.judge_llm,留空用引擎 LLM)。
任何失败(未配置/超时/HTTP 错误/输出不可解析)→ None,调用方只用 Jev。
供应商需要的额外请求体字段(如 GLM 关闭思考 {"thinking": {"type": "disabled"}})配在该模型的参数里;
旧的 CE_JUDGE_LLM_MODEL / CE_JUDGE_LLM_EXTRA 环境变量仍然有效。
"""

from __future__ import annotations

import json
import logging
import re

from app import settings
from app.config import LLMConfig
from app.core import Enzyme
from app.llm_client import complete

logger = logging.getLogger(__name__)

# 与离线评测逐字一致——改措辞等于换判官,融合效果需重新验证。
_SYSTEM = ("你是相关性判定器。对每个候选,给出它对处理用户请求『直接有用』的概率(0 到 1 的小数)。"
           "只输出 JSON 对象,键为候选编号,值为概率,不要任何其他文字。")
_QUESTION = "`rule` 的建议对处理 state 中这个请求是否直接有用?"
_CRITERIA = ("判定标准:请求所处的场景正是该规则针对的场景,agent 照做会把事情做得更好 → 接近 1;"
             "场景不同、只是关键词相似,或规则与请求无关 → 接近 0。")

def judge_llm_enabled(config: LLMConfig | None) -> bool:
    return bool(config and config.available and settings.get("judge.llm"))


def judge_llm_fallback_enabled(config: LLMConfig | None) -> bool:
    """judge.llm_fallback(默认开):Jev 失败/超时时由大模型接手,对同一候选池做适用性判定。"""
    return bool(config and config.available and settings.get("judge.llm_fallback"))


def _prompt(context: str, enzymes: list[Enzyme]) -> str:
    cands = "\n".join(
        f"[c{i}] " + json.dumps({"触发": e.patterns_readable, "建议": e.then, "依据": e.because},
                                ensure_ascii=False)
        for i, e in enumerate(enzymes))
    return f"{_QUESTION}\n{_CRITERIA}\n\n# state\n{context}\n\n# candidates\n{cands}"


def parse_probs(text: str, enzymes: list[Enzyme]) -> dict[str, float] | None:
    """从 LLM 输出里取 {c0: p, ...};缺项记 0,完全取不到返回 None。"""
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return None
    try:
        raw = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(raw, dict) or not raw:
        return None
    out = {}
    for i, e in enumerate(enzymes):
        val = raw.get(f"c{i}", raw.get(f"[c{i}]", 0.0))
        try:
            out[e.id] = max(0.0, min(1.0, float(val)))
        except (TypeError, ValueError):
            out[e.id] = 0.0
    return out


async def judge_llm(
    context: str,
    enzymes: list[Enzyme],
    config: LLMConfig,
    timeout: float = 8.0,
) -> dict[str, float] | None:
    """列表式判定:返回 {enzyme_id: 直接有用的概率},失败返回 None。"""
    if not enzymes or not config.available:
        return None
    text = await complete(config, [{"role": "user", "content": _prompt(context, enzymes)}], system=_SYSTEM,
                          temperature=0, max_tokens=settings.get("judge.llm_max_tokens"), timeout=timeout)
    if text is None:
        return None
    probs = parse_probs(text, enzymes)
    if probs is None:
        logger.warning("LLM judge output unparsable: %s", text[:120])
    return probs

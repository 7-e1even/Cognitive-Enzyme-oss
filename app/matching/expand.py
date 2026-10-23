"""查询扩写(HyDE 式)—— 召回前把具体情境抽象成"哪一类已存知识最有用",再拿它检索。

记忆写的是抽象知识(CWE 弱点类型、法条原文、研究结论),情境是具体描述(某个 CVE、某段
判决、某条论断),两者在向量空间里常常离得远——真实基准里 35% 的 miss 是"根本没召回"。
扩写与原查询的 embedding 检索并行,两路召回按酶取最高分合并。经验样本解决"见过的情境",
扩写解决"第一次遇到的情境"。

默认关闭(运行参数 recall.query_expand);用绑定的「查询扩写模型」(model.query_expand,留空用引擎 LLM),
任何失败都退回只用原查询。供应商需要的额外请求体字段(如 GLM 关闭思考 {"thinking": {"type": "disabled"}})
配在该模型的参数里;旧的 CE_QUERY_EXPAND_MODEL / CE_QUERY_EXPAND_EXTRA 环境变量仍然有效。
"""

from __future__ import annotations

import logging

from app import settings
from app.config import LLMConfig
from app.llm_client import complete

logger = logging.getLogger(__name__)

# 与离线实验(tests/eval/realbench/run_expand.py)逐字一致;与领域无关——线上事先不知道情境属于哪个领域
PROMPT = ("You are helping a memory system recall relevant stored knowledge. Read the situation below and write "
          "a short description (2-4 sentences) of the kind of stored knowledge that would be most useful for it: "
          "the underlying principle, rule, known issue, weakness type, or research finding, stated generally, the "
          "way a knowledge-base entry would state it. Do not solve the problem. Output only the description.\n\n"
          "Situation:\n{text}")


def expand_on(config: LLMConfig | None) -> bool:
    return bool(config and config.available and settings.get("recall.query_expand"))


async def expand_query(context: str, config: LLMConfig, timeout: float = 8.0) -> str | None:
    """返回情境的抽象描述;失败返回 None(调用方只用原查询)。"""
    text = await complete(config, [{"role": "user", "content": PROMPT.format(text=context[:3000])}],
                          temperature=0, max_tokens=300, timeout=timeout)
    if text is None:
        logger.warning("query expansion failed")
        return None
    return text.strip() or None

"""已会过滤 — 模型闭卷就会的经验不入库。

提炼出来的候选里有一类是教科书方法论(先算简单情形、分阶段推进、拆开条件逐条看……),预训练里早就有,
存进记忆库只会占注入名额、稀释真正有价值的经验(踩过的坑、项目约定、做错之后才知道的东西)。

做法:让引擎 LLM 只看这条经验的触发情境、不给任何记忆,闭卷答一遍"你会怎么做";再判断这份答案是否已经
涵盖了候选经验的核心可执行要点。涵盖了就认为模型自己会,不入库。任何一步失败都按"不认识"处理,照常入库。
"""

from __future__ import annotations

import logging
import re

from app import settings
from app.llm_client import chat, chat_json

logger = logging.getLogger(__name__)

_ANSWER_CLIP = 2000

_SOLVE_SYSTEM = "你是一位经验丰富的助手。只凭你自己已有的知识回答,简明扼要,给出具体做法。"

_JUDGE_SYSTEM = "你是严格的评审,判断一份闭卷回答是否已经包含某条经验的要点。"

_JUDGE_PROMPT = """下面是一条候选经验,以及一位没有看过这条经验的助手,针对同一情境闭卷写的回答。

情境:{trigger}

候选经验(该做什么):{then}

闭卷回答:
\"\"\"
{answer}
\"\"\"

判断:闭卷回答是否已经把候选经验的核心可执行要点当作明确建议给了出来(意思到位即可,不要求字面一样)?
- 候选经验里有闭卷回答没提到的具体条件、关键步骤、反直觉的结论或特定的坑 → covered 为 false
- 候选经验含有只在特定环境或项目里成立的具体事实(地址、端口、路径、版本、命令参数、配置值、命名约定等),闭卷回答没有给出同样的具体内容 → covered 为 false
- 闭卷回答只是零散沾边,要从多处拼凑、或要往宽了解读才对得上 → covered 为 false
- 闭卷回答讨论的情境和候选经验针对的不是同一类问题 → covered 为 false
- 只是把常识说得更详细、更有条理,闭卷回答已经给出同样的做法 → covered 为 true
拿不准时判 false。

返回 JSON 对象:{{"covered": true 或 false, "reason": "一句话理由"}}"""

# 判官 fail-open 的显性原因(dry-run 报告据此统计失败分布)
_REASON_NO_TRIGGER = "no-trigger"
_REASON_NO_ANSWER = "no-answer"
_REASON_ERROR = "error"
_REASON_BAD_VERDICT = "bad-verdict"


def enabled() -> bool:
    """开关:learning.skip_known。"""
    return bool(settings.get("learning.skip_known"))


async def judge(cand: dict) -> tuple[bool, str]:
    """闭卷判定:返回 (已会, 原因)。任何一步失败都 fail-open,原因显式区分。"""
    trigger = (cand.get("trigger") or "").strip()
    then = (cand.get("then") or "").strip()
    if not trigger or not then:
        return False, _REASON_NO_TRIGGER
    try:
        answer = await chat(system=_SOLVE_SYSTEM, user=f"遇到下面的情况,你会怎么做?\n\n{trigger}", timeout=60.0)
        if not answer:
            return False, _REASON_NO_ANSWER
        verdict = await chat_json(
            system=_JUDGE_SYSTEM,
            user=_JUDGE_PROMPT.format(trigger=trigger, then=then, answer=answer[:_ANSWER_CLIP]),
        )
    except Exception:
        logger.exception("known filter failed (non-fatal)")
        return False, _REASON_ERROR
    if not isinstance(verdict, dict):
        return False, _REASON_BAD_VERDICT
    return verdict.get("covered") is True, str(verdict.get("reason") or "")


async def already_known(cand: dict) -> bool:
    """候选经验的要点,模型闭卷是否已经会。失败一律返回 False(不拦,照常入库)。"""
    return (await judge(cand))[0]


# 「……时」误判守卫:这些字结尾的「X时」是副词不是情境从句收尾(同时/此时/及时……)
_NOT_TIME_PREV = set("同此及随平暂有按准实适届那这临耗历费")
_CLAUSE_END = re.compile(r"时(?=[：:，,；;）)（(—\s])")


def _situation_clause(then: str) -> str:
    """then 开头的「……时」情境从句(候选经验多写成「情境时:做法」)。"""
    t = (then or "").strip()
    if t.startswith(("（", "(")):          # capture UPDATE 留下的「(保留原 then 全文…)」补丁头
        return ""
    for m in _CLAUSE_END.finditer(t[:200]):
        i = m.start()
        if i >= 4 and t[i - 1] not in _NOT_TIME_PREV:
            return t[: i + 1]
    return ""


def enzyme_trigger(enzyme) -> str:
    """重建一条已有酶的触发情境(回填已会过滤用)。

    优先取 then 开头的「……时」情境从句(候选经验多写成「情境时:做法」,天生与 then 同域);
    再从 because 的「从实际任务中发现: 」前缀还原(harvest 写入时的格式);然后第一条非空
    经验样本(可能是后学到的情境,放后面);最后退化到 模式值 + 触发条件 的拼接。
    """
    clause = _situation_clause(enzyme.then)
    if clause:
        return clause
    because = enzyme.because or ""
    if because.startswith("从实际任务中发现:"):
        return because[len("从实际任务中发现:"):].strip()
    for ex in enzyme.exemplars or []:
        if ex and ex.strip():
            return ex.strip()
    parts = [p.value for p in (enzyme.patterns or []) if p.value and p.value.strip()]
    parts += [c for c in (enzyme.conditions or []) if c and c.strip()]
    return "；".join(parts)


async def skip_enzyme(enzyme) -> bool:
    """整条已有酶(白日梦/交叉等新产出)是否模型已会。开关没开或判不动都不拦。"""
    return enabled() and await already_known(
        {"trigger": enzyme_trigger(enzyme), "then": enzyme.then}
    )

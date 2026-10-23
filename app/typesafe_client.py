"""TypeSafe client — System One(Jev)结构化判定。

Jev 不生成文本:给一段 state 和一组带类型的问题(noul=是/否概率、choice=多选一分布),
返回校准过的概率。这里只用它做 prime 热路径上的"记忆适用性"判定(见 pipeline Stage 4.5):

- 每条候选一个 noul:"这条规则对这个请求是否直接有用"——绝对适用性;
- 同一请求再加一道 choice:"哪条最有用 / 都没用"——相对比较。逐条独立的 noul 容易
  把一簇近邻都判"有用",choice 补上相对维度;fan-out 在同一请求里,不加请求数。

离线评测(tests/eval/jev_gate/README.md):choice 主选 + 高 noul 补充在自家集与 TheoremQA /
MetaTool 上精度与静默率均显著优于只用 noul,且不输 LLM 列表式判定,延迟约为其一半。

失败(未配置/超时/HTTP 错误/响应缺字段)一律返回 None,由调用方回退原剂量逻辑——热路径
上绝不因 Jev 抛错。热路径基本不重试:hook 端总预算只有几秒,重试只会把一次失败变成超时;
唯一例外是并发限流(HTTP 429)——那是瞬时拥堵,稍等就放行。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import weakref
from dataclasses import dataclass, field

import httpx

from app import settings
from app.config import TypeSafeConfig
from app.core import Enzyme

logger = logging.getLogger(__name__)

# 提示语与离线评测保持逐字一致——改措辞等于换了一个判定器,阈值需重新标定。
# v1:为"建议类"记忆设计("照做会不会更好")。v2:覆盖建议与知识——处理这个情境时 agent
# 应不应该知道这条记忆,无论它支持、反驳还是补充情境里的说法(反驳性证据恰是最该提醒的)。
# 真实基准上 v1 把 SciFact 里反驳论断的摘要大量判为"没用"(biomed recall 65.7%→51.7%)。
_PROMPTS = {
    "v1": {
        "keys": ("触发", "建议", "依据"),
        "noul": "`rule` 的建议对处理 state 中这个请求是否直接有用?",
        "true": "请求所处的场景正是该规则针对的场景,agent 照做会把事情做得更好",
        "false": "场景不同、只是关键词相似,或规则与请求无关",
        "choice": "以下规则中,哪一条对处理 state 中这个请求最直接有用?如果没有一条直接有用,选 none。",
        "none": "没有任何一条规则对这个请求直接有用",
    },
    "v2": {
        "keys": ("适用情境", "内容", "依据"),
        "noul": "处理 state 中的这个情境时,agent 应不应该知道 `rule` 这条记忆——它与这个情境直接相关吗?",
        "true": "它讲的正是这个情境涉及的具体问题、事实或做法(无论是支持、反驳还是补充情境里的说法)",
        "false": "只是主题相近或关键词相似,与这个情境要解决的具体问题无关",
        "choice": "以下记忆中,哪一条与处理 state 中这个情境最直接相关?如果没有一条直接相关,选 none。",
        "none": "没有任何一条记忆与这个情境直接相关",
    },
}


# v3:逐条 noul 用 v1(照做会不会更好——绝对判断要严,否则一簇近邻都判"相关"),
# 单选题用 v2(哪条最相关——相对比较时知识类记忆也能被选中)。
_MIXED = {"v3": ("v1", "v2")}


def _prompt_pair() -> tuple[dict, dict]:
    """(noul 用的问法, choice 用的问法)。"""
    name = settings.get("judge.prompt")
    noul, choice = _MIXED.get(name, (name, name))
    return _PROMPTS.get(noul, _PROMPTS["v1"]), _PROMPTS.get(choice, _PROMPTS["v1"])


def _prompt() -> dict:
    return _prompt_pair()[0]

_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    """Lazy singleton httpx client(与 llm_client / rerank 同款模式)。"""
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=10.0)
    return _client


@dataclass
class Applicability:
    """一次适用性判定的结果,键为 enzyme id。"""
    p: dict[str, float]      # noul:该条对请求直接有用的概率(绝对)
    q: dict[str, float]      # choice:该条是"最有用那条"的概率(相对,与 none 合计为 1)
    none: float              # choice 判"没有一条有用"的概率
    model: str = ""
    # 类比识别(见 _ANALOGY):当前情境与该记忆最像的过往适用情境是不是"同一类情况"
    analog_p: dict[str, float] = field(default_factory=dict)   # noul(绝对)
    analog_q: dict[str, float] = field(default_factory=dict)   # choice(相对,与 analog_none 合计为 1)
    analog_none: float = 1.0
    # 原样的请求与作答、判定池顺序(id, 版本)——判定记录(app/distill/records.py)用,可重放给任何老师
    request: dict | None = field(default=None, repr=False)
    raw: dict | None = field(default=None, repr=False)
    pool: list = field(default_factory=list, repr=False)
    mode: str = "single"                    # "single" 一次联合判定;"two_stage" 先扫后比
    calls: int = 1                          # 本次判定发出的 system_one 请求数
    scan: dict[str, float] = field(default_factory=dict)   # two_stage 第一阶段的逐条概率
    supplement_q: dict[str, float] = field(default_factory=dict)  # two_stage 新扫描候选的独立 choice 概率


# 类比识别(System 1 的"见过就认得"):不问抽象记忆对请求有没有用,而问当前情境和它过去
# 适用过的某个真实情境是不是同一类情况。离线(tests/eval/realbench/analogy_offline.py,真实经验流,
# 过去情境按 0.5 概率学会):单选 ≥0.7 且逐条 ≥0.6 时,法律流精度 89%、认回 40% 熟悉情境,
# Firefox 流 83% / 37%;现有适用性判定下熟悉情境命中约 30%。
_ANALOGY = {
    "noul": "state 里的情境,和 `past` 这条过去的情境,是不是同一类情况——处理它们需要的是同一条知识?",
    "true": "两者针对的是同一个具体问题、同一条规则或同一个已知缺陷,只是表述不同",
    "false": "只是主题相近或用词相似,实际需要的知识不同",
    "choice": "以下过去的情境中,哪一条和 state 里的情境是同一类情况、需要同一条知识?如果都不是,选 none。",
    "none": "都不是同一类情况",
}


# 判官单次请求有输入 token 硬上限(bocha-jev-v1:expanded_input_tokens ≤ 32768,超出直接
# 422、整次判定作废)。下面只裁剪"发给判官的内容"(存库原文不动):字段截断 +
# judge.token_budget 按精排序截判定池。
_RULE_THEN_MAX = 400
_RULE_WHY_MAX = 150
_STATE_MAX = 1500
_OPTION_DESC_MAX = 80        # 单选题选项里的描述部分(领域分流 / 领域归类的领域描述;名称不截)
_CONTEXT_OPTION_MAX = 400    # pick_context 每个候选情境选项


def _estimate_tokens(n: int, state_chars: int, rule_chars: int) -> float:
    """估算一次 systemone 请求的 expanded_input_tokens。

    n = 逐条题数(候选 c{i} 与类比 a{k} 各计一题),state_chars = len(state),
    rule_chars = 各题 instructions.rule / past 文本的 JSON 长度合计。系数来自对
    bocha-jev-v1 线上接口的实测标定(15 次成功调用,最大相对误差 11%)。"""
    return 96 * n + 0.736 * n * state_chars + 3.063 * rule_chars + 0.726 * n * rule_chars


def _rule(enzyme: Enzyme, pr: dict | None = None) -> dict:
    k_trigger, k_body, k_why = (pr or _prompt())["keys"]
    rule = {k_trigger: enzyme.patterns_readable, k_body: enzyme.then[:_RULE_THEN_MAX],
            k_why: enzyme.because[:_RULE_WHY_MAX]}
    # 召回时命中的最像的过往适用情境(经验样本):给判定一个类比锚点——"这和上次那次很像"
    if settings.get("exemplar.hint"):
        if enzyme._hint:
            rule["过往适用情境"] = enzyme._hint[:400]
        if enzyme._anti_hint:
            rule["过往不适用情境"] = enzyme._anti_hint[:400]
    return rule


# ── 熔断:鉴权 / 计费失败(401/402/403)重试不会好转——key 无效、没有权限或额度用完。
# 按"地址 + key"暂停调用,期间直接返回 None(和调用失败时一样,由调用方回退),不再每次都打过去。
# 冷却从 60 秒起,连续失败翻倍,最长 10 分钟;换了 key 立刻恢复,调用成功就清零。
_AUTH_FAILURES = {401, 402, 403}
_COOLDOWN_MIN, _COOLDOWN_MAX = 60.0, 600.0
_breaker: dict[str, tuple[float, float]] = {}   # key 指纹 → (恢复时刻, 当前冷却秒数)
_now = time.monotonic


def _key_id(config: TypeSafeConfig) -> str:
    return hashlib.sha256(f"{config.api_url}|{config.api_key}".encode()).hexdigest()[:16]


# 账号并发上限是 10(超出直接 429):进程内用信号量限并发。asyncio 原语绑 event loop,
# 测试/多 loop 场景按 loop 各建一把;配置改了就在下次调用时重建。
_sems: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()   # loop → (并发上限, Semaphore)
_429_BACKOFF = (0.5, 1.0)   # 限流退避:瞬时拥堵,稍等再发;不计入熔断


def _semaphore() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    n = settings.get("judge.concurrency")
    got = _sems.get(loop)
    if got is None or got[0] != n:
        got = (n, asyncio.Semaphore(n))
        _sems[loop] = got
    return got[1]


async def system_one(
    state: str | dict | list,
    questions: dict,
    config: TypeSafeConfig,
    timeout: float = 10.0,
) -> dict | None:
    """POST /v1/systemone。返回完整响应 JSON(含 answers/model/usage),失败返回 None。

    HTTP 429(账号并发上限)按 _429_BACKOFF 退避至多 2 次;鉴权/计费失败走熔断。"""
    if not config.available or not questions:
        return None
    kid = _key_id(config)
    until, cooldown = _breaker.get(kid, (0.0, 0.0))
    if _now() < until:
        return None
    url = f"{config.api_url.rstrip('/')}/systemone"
    for attempt in range(len(_429_BACKOFF) + 1):
        try:
            async with _semaphore():
                resp = await _get_client().post(
                    url,
                    headers={"Authorization": f"Bearer {config.api_key}",
                             "Content-Type": "application/json"},
                    json={"model": config.model, "state": state, "questions": questions},
                    timeout=timeout,
                )
            resp.raise_for_status()
            _breaker.pop(kid, None)
            return resp.json()
        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            if status in _AUTH_FAILURES:
                cooldown = min(max(cooldown * 2, _COOLDOWN_MIN), _COOLDOWN_MAX)
                _breaker[kid] = (_now() + cooldown, cooldown)
                # 端点配置错误时可能把请求内容(含 Authorization)回显进响应体:脱敏后再入日志
                body = e.response.text[:300]
                if config.api_key and config.api_key in body:
                    body = body.replace(config.api_key, "***")
                logger.error("TypeSafe 鉴权/计费失败(HTTP %s),暂停调用 %d 秒:%s",
                             status, cooldown, body)
                return None
            if status == 429 and attempt < len(_429_BACKOFF):
                logger.warning("TypeSafe 并发限流(HTTP 429),%.1fs 后重试", _429_BACKOFF[attempt])
                await asyncio.sleep(_429_BACKOFF[attempt])
                continue
            logger.warning("TypeSafe call failed: %s", e)
            return None
        except Exception as e:
            logger.warning("TypeSafe call failed: %s", e)
            return None
    return None


def fit_pool(context: str, enzymes: list[Enzyme]) -> list[Enzyme]:
    """按 judge.token_budget 估算截判定池:返回精排序最长的、估算不超预算的前缀
    (至少留 1 条;0 = 不截)。Jev 与接手的大模型判同一个池。"""
    budget = settings.get("judge.token_budget")
    if budget <= 0 or len(enzymes) <= 1:
        return enzymes
    pn = _prompt_pair()[0]
    state_chars = len(context[:_STATE_MAX])
    kept: list[Enzyme] = []
    rule_chars = 0
    for e in enzymes:
        rc = len(json.dumps(_rule(e, pn), ensure_ascii=False))
        if kept and _estimate_tokens(len(kept) + 1, state_chars, rule_chars + rc) > budget:
            break
        kept.append(e)
        rule_chars += rc
    if len(kept) < len(enzymes):
        logger.debug("判官请求超 token 预算:候选 %d → %d 条", len(enzymes), len(kept))
    return kept


def fit_options(state, options: dict, prefix: str, keep: int = 1) -> int:
    """单题请求(领域分流 / 领域归类 / pick_context 等,每请求一道 choice)的预算裁剪:
    从尾部丢 ``prefix`` 开头的选项,直到估算不超 judge.token_budget(0 = 不限)。

    ``options`` 形如 {prefix0..prefix{n-1}, "none"}——估算按 n=1、R=criteria 的 JSON 长度
    (标定公式来自「n 逐条 + 1 单选」的请求,用在单题上是近似值,做截断依据足够)。与
    fit_pool 同一条规矩:至少留 ``keep`` 个选项,预算再小也照发。返回丢掉的选项数,
    调用方据此截短自己的候选列表、写 debug 日志。"""
    budget = settings.get("judge.token_budget")
    if budget <= 0:
        return 0
    chars = len(state) if isinstance(state, str) else len(json.dumps(state, ensure_ascii=False))
    dropped = 0
    while len(options) > keep + 1 and \
            _estimate_tokens(1, chars, len(json.dumps(options, ensure_ascii=False))) > budget:
        del options[f"{prefix}{len(options) - 2}"]
        dropped += 1
    return dropped


def _noul_questions(enzymes: list[Enzyme], pn: dict) -> dict:
    """逐条 noul 题(联合判定与两段判定扫视共用,题面保证逐字一致)。"""
    return {f"c{i}": {
        "type": "noul",
        "instructions": {"rule": _rule(e, pn), "question": pn["noul"]},
        "criteria": {"true": pn["true"], "false": pn["false"]},
    } for i, e in enumerate(enzymes)}


async def judge_applicability(
    context: str,
    enzymes: list[Enzyme],
    config: TypeSafeConfig,
    timeout: float = 10.0,
    analogies: list[Enzyme] | None = None,
) -> Applicability | None:
    """对一批候选酶判定"对这个请求是否直接有用"(每条一个 noul + 一道 choice)。

    ``analogies``:带过往适用情境(``_hint``)的候选,同一请求里再问类比识别(每条一个 noul +
    一道 choice)。Jev 各题独立作答,加题不改变适用性判定的结果(实测差值 ≤0.02)。"""
    if not enzymes:
        return None
    pn, pc = _prompt_pair()
    state = context[:_STATE_MAX]
    budget = settings.get("judge.token_budget")
    enzymes = fit_pool(context, enzymes)
    questions = _noul_questions(enzymes, pn)
    options: dict = {f"c{i}": _rule(e, pc) for i, e in enumerate(enzymes)}
    options["none"] = pc["none"]
    questions["best"] = {"type": "choice", "instructions": pc["choice"], "criteria": options}
    analogies = [e for e in (analogies or []) if e._hint]
    if analogies and budget > 0:
        # 候选判完再按剩余预算收类比题——类比先丢:它只是补充证据,候选被截就永远选不中。
        n = len(enzymes)
        rule_chars = sum(len(json.dumps(_rule(e, pn), ensure_ascii=False)) for e in enzymes)
        kept_a: list[Enzyme] = []
        for e in analogies:
            rc = len(e._hint[:800])
            if _estimate_tokens(n + len(kept_a) + 1, len(state), rule_chars + rc) > budget:
                break
            kept_a.append(e)
            rule_chars += rc
        if len(kept_a) < len(analogies):
            logger.debug("Jev 请求超 token 预算:类比 %d → %d 条", len(analogies), len(kept_a))
        analogies = kept_a
    if analogies:
        past = {f"a{k}": e._hint[:800] for k, e in enumerate(analogies)}
        for key, text in past.items():
            questions[key] = {
                "type": "noul",
                "instructions": {"past": text, "question": _ANALOGY["noul"]},
                "criteria": {"true": _ANALOGY["true"], "false": _ANALOGY["false"]},
            }
        questions["same"] = {"type": "choice", "instructions": _ANALOGY["choice"],
                             "criteria": {**past, "none": _ANALOGY["none"]}}

    data = await system_one(state, questions, config, timeout=timeout)
    if not data:
        return None
    try:
        answers = data["answers"]
        p = {e.id: float(answers[f"c{i}"]["noul"]) for i, e in enumerate(enzymes)}
        probs = answers["best"]["probabilities"]
        q = {e.id: float(probs.get(f"c{i}", 0.0)) for i, e in enumerate(enzymes)}
        none = float(probs.get("none", 0.0))
    except (KeyError, TypeError, ValueError) as e:
        logger.warning("TypeSafe response malformed: %s", e)
        return None
    app = Applicability(p=p, q=q, none=none, model=str(data.get("model", "")),
                        request={"state": state, "questions": questions}, raw=answers,
                        pool=[[e.id, e.version] for e in enzymes])
    if analogies:
        try:  # 类比题缺失/畸形只丢类比结果,不影响适用性判定
            probs = answers["same"]["probabilities"]
            app.analog_p = {e.id: float(answers[f"a{k}"]["noul"]) for k, e in enumerate(analogies)}
            app.analog_q = {e.id: float(probs.get(f"a{k}", 0.0)) for k, e in enumerate(analogies)}
            app.analog_none = float(probs.get("none", 0.0))
        except (KeyError, TypeError, ValueError) as e:
            logger.warning("TypeSafe analogy answers malformed: %s", e)
    return app


def _scan_chunks(context: str, enzymes: list[Enzyme], pn: dict) -> list[list[Enzyme]]:
    """把候选切成扫视批:每批至多 judge.scan_batch 条;加上下一条会让该批的 token 估算
    超 judge.token_budget 就提前封批(0 = 不限;每批至少留 1 条)。"""
    batch = settings.get("judge.scan_batch")
    budget = settings.get("judge.token_budget")
    state_chars = len(context[:_STATE_MAX])
    chunks: list[list[Enzyme]] = []
    chunk: list[Enzyme] = []
    rule_chars = 0
    for e in enzymes:
        rc = len(json.dumps(_rule(e, pn), ensure_ascii=False))
        if chunk and (len(chunk) >= batch or
                      (budget > 0 and _estimate_tokens(len(chunk) + 1, state_chars,
                                                     rule_chars + rc) > budget)):
            chunks.append(chunk)
            chunk, rule_chars = [], 0
        chunk.append(e)
        rule_chars += rc
    if chunk:
        chunks.append(chunk)
    return chunks


async def scan_applicability(
    context: str,
    enzymes: list[Enzyme],
    config: TypeSafeConfig,
    timeout: float = 10.0,
) -> dict[str, float] | None:
    """两段判定·第一阶段:并发分批逐条 noul 扫候选(不含单选/类比题)。

    实测单独判与塞在联合请求里判的 noul 概率几乎一致(中位差 0.000,0/230 次阈值翻转),
    拆开扫的意义正是绕过单请求 token 上限、看到被 fit_pool 截掉的候选。某批失败/畸形
    只丢那批结果;全部失败才返回 None。"""
    if not enzymes:
        return None
    pn = _prompt_pair()[0]
    state = context[:_STATE_MAX]
    chunks = _scan_chunks(context, enzymes, pn)

    async def _one(chunk: list[Enzyme]) -> dict[str, float]:
        data = await system_one(state, _noul_questions(chunk, pn), config, timeout=timeout)
        if not data:
            return {}
        try:
            answers = data["answers"]
            return {e.id: float(answers[f"c{i}"]["noul"]) for i, e in enumerate(chunk)}
        except (KeyError, TypeError, ValueError) as e:
            logger.warning("TypeSafe scan response malformed: %s", e)
            return {}

    merged: dict[str, float] = {}
    for part in await asyncio.gather(*(_one(c) for c in chunks)):
        merged.update(part)
    return merged or None


async def judge_two_stage(
    context: str,
    enzymes: list[Enzyme],
    config: TypeSafeConfig,
    timeout: float = 10.0,
    analogies: list[Enzyme] | None = None,
) -> Applicability | None:
    """先分批取得 p,再比较原单批候选及扫描 top-k,第二阶段不重复询问 p。

    原单批候选先放入比较池,新增候选在单题预算内补入,避免重排后再次 fit_pool
    裁掉原本能靠 choice 入选的低 p 候选。request/raw 保存实际第二阶段请求,
    c{i} 始终对应返回的扫描池顺序,供判定记录和老师重放。"""
    if settings.get("judge.two_stage_compare") == "joint":
        return await _judge_two_stage_joint(context, enzymes, config, timeout, analogies)
    t0 = time.monotonic()
    original = fit_pool(context, enzymes)
    scan = list(enzymes[:settings.get("judge.scan_pool")])
    have = {e.id for e in scan}
    analogies = [e for e in (analogies or []) if e._hint]
    for e in original + analogies:
        if e.id not in have:
            scan.append(e)
            have.add(e.id)
    reuse_fit = settings.get("judge.scan_reuse_fit")
    if reuse_fit:
        # The incumbent prefix already fits the original joint request. Reusing
        # that p/q avoids spending scan calls to re-ask the same candidates; the
        # extra budget is spent only on candidates that were previously hidden.
        remaining = max(0.5, timeout - (time.monotonic() - t0))
        app = await judge_applicability(context, original, config, timeout=remaining,
                                        analogies=analogies) if original else None
        original_ids = {e.id for e in original}
        # Analogy candidates are also part of the overflow scan: their
        # separate analogy q does not replace the normal p evidence when the
        # candidate is outside the incumbent prefix.
        scan_extras = [e for e in scan if e.id not in original_ids]
        remaining = max(0.5, timeout - (time.monotonic() - t0))
        scanned = await scan_applicability(context, scan_extras, config, remaining) if scan_extras else {}
        if scanned is None:
            scanned = {}
        p = dict(app.p) if app is not None else {}
        p.update(scanned)
        if not p:
            return None
        top = sorted((e for e in scan_extras if e.id in scanned),
                     key=lambda e: scanned[e.id], reverse=True)
        top = top[:settings.get("judge.compare_k")]
        extras = top
        if app is not None:
            app.scan = p.copy()
    else:
        p = await scan_applicability(context, scan, config, timeout)
        if p is None:
            return None
        top = sorted((e for e in scan if e.id in p), key=lambda e: p[e.id], reverse=True)
        top = top[:settings.get("judge.compare_k")]
        # The original fit_pool prefix is an incumbent set. Compare it in its own
        # choice request so adding scanned candidates cannot dilute its q or push a
        # previously selectable low-p/high-q memory out of the token budget.
        protected = [e for e in original if e.id in p]
        have = {e.id for e in protected}
        extras = [e for e in top if e.id not in have]
        remaining = max(0.5, timeout - (time.monotonic() - t0))
        app = await _compare_scanned(context, protected, scan, p, config, remaining, analogies=[]) if protected else None
    # A supplemental request judges only newly scanned candidates. Their q is
    # relative to the supplemental set; p remains the absolute scan score.
    if extras:
        remaining = max(0.5, timeout - (time.monotonic() - t0))
        supplement = await _compare_scanned(context, extras, scan, p, config, remaining, analogies=[])
        if supplement is not None:
            if app is None:
                # No protected candidate survived the scan. The supplemental
                # comparison is then the primary comparison and must not send
                # a choice request containing only `none`.
                app = supplement
            else:
                app.supplement_q = supplement.q
                app.supplement_raw = supplement.raw
                app.supplement_request = supplement.request
                app.none = min(app.none, supplement.none)
                app.has_supplement = True
    if app is None:
        return None

    # Adaptive overflow: spend the extra budget only when the first pass has
    # no candidate that can satisfy the existing selection gate. This targets
    # the observed judge_budget_cut bucket without turning every request into
    # the 36-candidate/high-call-cost path.
    adaptive_compare_calls = 0
    if settings.get("judge.adaptive_overflow"):
        q_values = dict(app.q)
        q_values.update(getattr(app, "supplement_q", {}))
        top_id = max(q_values, key=q_values.get, default=None)
        t = {
            "choice_min": settings.get("selection.choice_min"),
            "primary_p": settings.get("selection.primary_p"),
            "choice_override": settings.get("selection.choice_override"),
            "supplement_p": settings.get("selection.supplement_p"),
            "none_max": settings.get("selection.none_max"),
        }
        p_top = app.p.get(top_id, 0.0) if top_id is not None else 0.0
        q_top = q_values.get(top_id, 0.0) if top_id is not None else 0.0
        none_ok = app.none < t["none_max"]
        has_gate = ((q_top >= t["choice_min"] and p_top >= t["primary_p"])
                    or (none_ok and p_top >= t["supplement_p"])
                    or q_top >= t["choice_override"])
        overflow_limit = max(settings.get("judge.scan_pool"), settings.get("judge.overflow_pool"))
        if not has_gate and overflow_limit > settings.get("judge.scan_pool"):
            overflow = [e for e in enzymes[settings.get("judge.scan_pool"):overflow_limit]
                        if e.id not in {x.id for x in scan}]
            if overflow:
                remaining = max(0.5, timeout - (time.monotonic() - t0))
                overflow_p = await scan_applicability(context, overflow, config, remaining)
                if overflow_p:
                    p.update(overflow_p)
                    top_overflow = sorted((e for e in overflow if e.id in overflow_p),
                                          key=lambda e: overflow_p[e.id], reverse=True)
                    top_overflow = top_overflow[:settings.get("judge.compare_k")]
                    if top_overflow:
                        scan.extend(overflow)
                        if reuse_fit:
                            scan_extras.extend(overflow)
                        overflow_app = await _compare_scanned(
                            context, top_overflow, scan, p, config,
                            max(0.5, timeout - (time.monotonic() - t0)), analogies=[])
                        adaptive_compare_calls += 1
                        if overflow_app is not None:
                            app.supplement_q.update(overflow_app.q)
                            app.none = min(app.none, overflow_app.none)
                            app.supplement_raw = overflow_app.raw
                            app.supplement_request = overflow_app.request
                            app.has_supplement = True
    app.mode = "two_stage"
    if reuse_fit:
        app.calls = (1 + len(_scan_chunks(context, scan_extras, _prompt_pair()[0]))
                     + int(bool(getattr(app, "has_supplement", False))) + adaptive_compare_calls)
    else:
        app.calls = (len(_scan_chunks(context, scan, _prompt_pair()[0])) + 1
                     + int(bool(getattr(app, "has_supplement", False))) + adaptive_compare_calls)
    app.scan = p
    app.p = p
    app.pool = [[e.id, e.version] for e in scan]
    return app


async def _judge_two_stage_joint(
    context: str,
    enzymes: list[Enzyme],
    config: TypeSafeConfig,
    timeout: float,
    analogies: list[Enzyme] | None,
) -> Applicability | None:
    """judge.two_stage_compare=joint:分批逐条扫完 judge.scan_pool 条(类比候选另加),再按逐条概率取前
    judge.compare_k 条做一次单选比较,同一请求里问类比题。

    与 protect 方式的区别:不再把"单请求预算装得下的前缀"当成特权集合——那个前缀只是精排前 3–5 条,
    和是否适用无关;比较集合完全由逐条概率决定,所以精排靠后但确实适用的记忆也能拿到单选证据。"""
    t0 = time.monotonic()
    scan = list(enzymes[:settings.get("judge.scan_pool")])
    have = {e.id for e in scan}
    analogies = [e for e in (analogies or []) if e._hint]
    scan += [e for e in analogies if e.id not in have]
    p = await scan_applicability(context, scan, config, timeout)
    if p is None:
        return None
    top = sorted((e for e in scan if e.id in p), key=lambda e: p[e.id], reverse=True)
    top = top[:settings.get("judge.compare_k")]
    remaining = max(0.5, timeout - (time.monotonic() - t0))
    app = await _compare_scanned(context, top, scan, p, config, remaining, analogies=analogies)
    if app is None:
        return None
    app.mode = "two_stage"
    app.calls = len(_scan_chunks(context, scan, _prompt_pair()[0])) + 1
    app.scan = p
    app.p = p
    app.pool = [[e.id, e.version] for e in scan]
    return app


async def _compare_scanned(context, compared, scan, p, config, timeout, analogies):
    """只问 choice(及原有类比题);复用扫描 p,不放大请求预算。"""
    pc = _prompt_pair()[1]
    state = context[:_STATE_MAX]
    # Keep the original fit_pool prefix as the protected comparison set. New
    # scanned candidates are only admitted when they fit alongside it.
    local = {f"c{i}": e for i, e in enumerate(compared)}
    options = {key: _rule(e, pc) for key, e in local.items()}
    options["none"] = pc["none"]
    dropped = fit_options(state, options, "c")
    if dropped:
        compared = compared[:-dropped]
    keys = {e.id: f"c{i}" for i, e in enumerate(scan)}
    # fit_options uses contiguous keys; expose scan-pool keys only after fitting.
    retained = [local[key] for key in options if key != "none" and key in local]
    options = {keys[e.id]: _rule(e, pc) for e in retained}
    options["none"] = pc["none"]
    questions = {"best": {"type": "choice", "instructions": pc["choice"], "criteria": options}}
    budget = settings.get("judge.token_budget")
    kept = []
    chars = len(json.dumps(options, ensure_ascii=False))
    for e in analogies:
        rc = len(e._hint[:800])
        if budget > 0 and _estimate_tokens(2 + len(kept), len(state), chars + rc) > budget:
            break
        kept.append(e)
        chars += rc
    if kept:
        past = {f"a{i}": e._hint[:800] for i, e in enumerate(kept)}
        questions.update({k: {"type": "noul", "instructions": {"past": text, "question": _ANALOGY["noul"]},
                              "criteria": {"true": _ANALOGY["true"], "false": _ANALOGY["false"]}}
                          for k, text in past.items()})
        questions["same"] = {"type": "choice", "instructions": _ANALOGY["choice"],
                             "criteria": {**past, "none": _ANALOGY["none"]}}
    data = await system_one(state, questions, config, timeout=timeout)
    if not data:
        return None
    try:
        answers = data["answers"]
        probs = answers["best"]["probabilities"]
        app = Applicability(p=p, q={e.id: float(probs.get(keys[e.id], 0.0)) for e in retained},
                            none=float(probs.get("none", 0.0)), model=str(data.get("model", "")),
                            request={"state": state, "questions": questions}, raw=answers)
    except (KeyError, TypeError, ValueError) as exc:
        logger.warning("TypeSafe comparison response malformed: %s", exc)
        return None
    if kept:
        try:
            probs = answers["same"]["probabilities"]
            app.analog_p = {e.id: float(answers[f"a{i}"]["noul"]) for i, e in enumerate(kept)}
            app.analog_q = {e.id: float(probs.get(f"a{i}", 0.0)) for i, e in enumerate(kept)}
            app.analog_none = float(probs.get("none", 0.0))
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning("TypeSafe comparison analogy response malformed: %s", exc)
    return app


async def pick_context(
    enzyme: Enzyme,
    contexts: list[str],
    config: TypeSafeConfig,
    min_q: float = 0.5,
    timeout: float = 15.0,
) -> str | None:
    """一次会话里先后有多条请求,这条记忆最适用于哪一条?(capture 事后学经验样本用)

    一道 choice:state 是记忆本身,选项是会话里的各条请求 + none。最高项概率 ≥ min_q
    且高于 none 才采纳;挑不出来返回 None(调用方退回 LLM 概括的触发描述)。"""
    contexts = [c for c in contexts if c][:30]
    if not contexts:
        return None
    pr = _PROMPTS["v1"]
    k_trigger, k_body, k_why = pr["keys"]
    rule = {k_trigger: enzyme.patterns_readable, k_body: enzyme.then[:_RULE_THEN_MAX],
            k_why: enzyme.because[:_RULE_WHY_MAX]}
    state = {"memory": rule}
    options: dict = {f"s{i}": c[:_CONTEXT_OPTION_MAX] for i, c in enumerate(contexts)}
    options["none"] = "这条记忆对其中任何一条请求都不适用"
    dropped = fit_options(state, options, "s")
    if dropped:
        logger.debug("pick_context 超 token 预算:候选情境 %d → %d 条", len(contexts), len(contexts) - dropped)
    questions = {"where": {
        "type": "choice",
        "instructions": "state 是一条记忆。以下是 agent 在一次会话里先后收到的请求,这条记忆对处理哪一条"
                        "最直接有用?如果都不适用,选 none。",
        "criteria": options,
    }}
    data = await system_one(state, questions, config, timeout=timeout)
    try:
        probs = data["answers"]["where"]["probabilities"] if data else {}
        best = max((k for k in probs if k != "none"), key=lambda k: float(probs[k]), default=None)
        if best is None:
            return None
        qb = float(probs[best])
        if qb < min_q or qb <= float(probs.get("none", 0.0)):
            return None
        return contexts[int(best[1:])]
    except (KeyError, TypeError, ValueError, IndexError) as e:
        logger.warning("TypeSafe pick_context malformed: %s", e)
        return None


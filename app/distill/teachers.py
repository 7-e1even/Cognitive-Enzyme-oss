"""老师 / 判官接入层(SDK):任何模型,只要说 OpenAI、Anthropic 或 System One 三种协议之一,
配一条配置就能当老师、当影子判官、进擂台、接到 System 1 网关后面。

协议(TeacherSpec.kind):
- openai      OpenAI Chat Completions(POST {url}/chat/completions)。GLM、Kimi、DeepSeek、通义(DashScope
              兼容模式)、OpenRouter、本地 vLLM / Ollama 等都走这个。
- anthropic   Anthropic Messages API(POST {url}/v1/messages,x-api-key + anthropic-version)。Claude 官方,
              以及 GLM / Kimi 等提供的 Anthropic 兼容地址。
- system_one  TypeSafe 的 System One API(POST {url}/systemone):Jev,以及同协议的 laya-serve,直接转发。

大模型(openai / anthropic)拿不到逐选项的 logprobs,统一把"带类型的问题"渲染成一道提示,让模型逐题写出
校准概率(JSON),长素材去重编号引用;思考类模型的推理块自动忽略,只取正文。

问题格式与 /v1/systemone 一致:
  {"c0": {"type": "noul", "instructions": ..., "criteria": {"true": ..., "false": ...}},
   "best": {"type": "choice", "instructions": ..., "criteria": {"c0": ..., "none": ...}}}
答案统一成 System One API 的 answers 形状:
  {"c0": {"type": "noul", "noul": 0.82}, "best": {"type": "choice", "probabilities": {...}, "choice": "c0"}}

配置:
- 后台「模型」页(推荐):每个对话模型(openai / anthropic 协议)与判官模型(system_one)都自动成为
  同名老师,密钥存在服务商上;``jev`` 始终指当前绑定的适用性判官(未绑定时读 TYPESAFE_*)。
- 兼容旧写法:CE_TEACHERS_FILE(JSON 列表文件,见 teachers.example.json)/ CE_TEACHERS(同样的列表放在
  环境变量里),key_env 写保存 API key 的环境变量名;与后台同名时以后台为准。后台「从环境变量导入」
  可以把它们一键落进数据库。

Python 里直接用:
  from app.distill.teachers import get_teacher, ask
  ans = await ask("glm-5.3", state, questions)          # 注册表里的名字
  t = make_teacher(TeacherSpec(name="x", kind="anthropic", url="https://api.anthropic.com",
                               model="claude-sonnet-5", key_env="ANTHROPIC_API_KEY"))
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field, replace
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

PROTOCOLS = ("openai", "anthropic", "system_one")


@dataclass(frozen=True)
class TeacherSpec:
    name: str
    kind: str                      # openai | anthropic | system_one
    url: str                       # openai: .../v1;anthropic: 根地址(自动补 /v1/messages);system_one: .../v1
    model: str
    key_env: str = ""              # 保存 API key 的环境变量名(旧写法;后台配置的模型直接带 api_key)
    extra: dict = field(default_factory=dict)     # 额外请求体字段(如 GLM thinking、Claude thinking)
    headers: dict = field(default_factory=dict)   # 额外请求头(如 OpenRouter 的 HTTP-Referer)
    temperature: float | None = 0.0               # None = 不传(部分推理模型只接受默认温度)
    max_tokens: int = 16000        # 思考模型的推理也算在内
    timeout: float = 300.0
    concurrency: int = 2
    retries: int = 3
    api_version: str = "2023-06-01"               # anthropic-version 请求头
    api_key: str = field(default="", repr=False)  # 后台服务商上的密钥(优先于 key_env)


@dataclass
class TeacherAnswer:
    answers: dict | None           # System One API 的 answers 形状;失败为 None
    usage: dict = field(default_factory=dict)
    latency_s: float = 0.0
    error: str = ""


def _key(spec: TeacherSpec) -> str:
    if spec.api_key:
        return spec.api_key
    return os.environ.get(spec.key_env, "") if spec.key_env else ""


async def _post(spec: TeacherSpec, url: str, headers: dict, body: dict) -> dict:
    async with httpx.AsyncClient(timeout=spec.timeout) as client:
        resp = await client.post(url, headers={**headers, **spec.headers}, json=body)
    resp.raise_for_status()
    return resp.json()


class SystemOneTeacher:
    """TypeSafe 的 System One API(Jev / laya-serve):原样转发。"""

    def __init__(self, spec: TeacherSpec):
        self.spec = spec

    async def answer(self, state, questions: dict) -> TeacherAnswer:
        t0 = time.perf_counter()
        headers = {"Content-Type": "application/json"}
        if _key(self.spec):
            headers["Authorization"] = f"Bearer {_key(self.spec)}"
        last = ""
        for attempt in range(self.spec.retries):
            try:
                data = await _post(self.spec, f"{self.spec.url.rstrip('/')}/systemone", headers,
                                   {"model": self.spec.model, "state": state, "questions": questions})
                return TeacherAnswer(data.get("answers"), data.get("usage") or {}, time.perf_counter() - t0)
            except Exception as e:
                last = f"{type(e).__name__}: {e}"
                if attempt < self.spec.retries - 1:
                    await asyncio.sleep(2 * (attempt + 1))  # 最后一次失败直接返回,别白睡
        return TeacherAnswer(None, {}, time.perf_counter() - t0, last)


# ── 大模型老师:把带类型的问题渲染成一道提示,要求逐题给出校准概率 ─────────────

_SYSTEM = (
    "你是校准过的判定老师。给你一个 state(情境)和一组带类型的问题,逐题给出概率:\n"
    "- noul 题:答案为\"是\"的概率(0 到 1 的小数);\n"
    "- choice 题:每个选项的概率,所有选项(含 none)合计为 1。\n"
    "概率要诚实、校准:有把握时接近 0 或 1,拿不准时靠中间。仔细想清楚每一题,"
    "最后只输出一个 JSON 对象:键是题号,noul 题的值是数字,choice 题的值是 {选项: 概率}。不要输出其他文字。"
)
_LONG = 60   # 超过这个长度的素材去重编号引用


def _text(v) -> str:
    return v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)


def render_prompt(state, questions: dict) -> str:
    materials: list[str] = []
    index: dict[str, int] = {}

    def ref(v) -> str:
        t = _text(v)
        if len(t) <= _LONG:
            return t
        if t not in index:
            index[t] = len(materials)
            materials.append(t)
        return f"[M{index[t]}]"

    lines = []
    for qid, q in questions.items():
        typ = q.get("type", "noul")
        ins = q.get("instructions", "")
        if isinstance(ins, dict):
            body = "; ".join(f"{k} = {ref(v)}" for k, v in ins.items() if k != "question")
            ins_text = f"{ins.get('question', '')}  ({body})" if body else str(ins.get("question", ""))
        else:
            ins_text = str(ins)
        crit = q.get("criteria")
        if typ == "choice":
            opts = crit if isinstance(crit, dict) else {str(i): c for i, c in enumerate(crit or [])}
            opt_text = "; ".join(f"{k} = {ref(v)}" for k, v in opts.items())
            lines.append(f"[{qid}] (choice) {ins_text}\n    选项: {opt_text}")
        else:
            crit_text = ""
            if isinstance(crit, dict):
                crit_text = f"\n    判为\"是\":{crit.get('true', '')};判为\"否\":{crit.get('false', '')}"
            lines.append(f"[{qid}] (noul) {ins_text}{crit_text}")
    mat = "\n".join(f"[M{i}] {t}" for i, t in enumerate(materials))
    return (f"# state\n{_text(state)}\n\n" + (f"# 素材\n{mat}\n\n" if mat else "")
            + "# 问题\n" + "\n".join(lines))


def _json_object(text: str) -> dict | None:
    """取输出里第一个能解析的顶层 JSON 对象(模型偶尔在正文前留几句话)。"""
    text = text or ""
    for m in re.finditer(r"\{", text):
        depth = 0
        for i in range(m.start(), len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    chunk = text[m.start():i + 1]
                    try:
                        obj = json.loads(re.sub(r",\s*([}\]])", r"\1", chunk))
                        if isinstance(obj, dict):
                            return obj
                    except json.JSONDecodeError:
                        pass
                    break
    return None


def parse_answers(text: str, questions: dict) -> dict | None:
    """把模型输出整理成 System One API 的 answers;一题都取不到返回 None,取不到的单题省略。"""
    raw = _json_object(text)
    if raw is None:
        return None
    out: dict = {}
    for qid, q in questions.items():
        val = raw.get(qid, raw.get(f"[{qid}]"))
        if val is None:
            continue
        try:
            if q.get("type") == "choice":
                crit = q.get("criteria")
                keys = list(crit) if isinstance(crit, dict) else [str(i) for i in range(len(crit or []))]
                if not isinstance(val, dict):
                    continue
                probs = {k: max(0.0, float(val.get(k, 0.0) or 0.0)) for k in keys}
                total = sum(probs.values())
                if total <= 0:
                    continue
                probs = {k: v / total for k, v in probs.items()}
                out[qid] = {"type": "choice", "probabilities": probs, "choice": max(probs, key=probs.get)}
            else:
                out[qid] = {"type": "noul", "noul": max(0.0, min(1.0, float(val)))}
        except (TypeError, ValueError):
            continue
    return out or None


class _LLMTeacher:
    """大模型老师的公共部分:渲染提示、重试、解析;子类只管协议差异(请求体与响应取文本)。"""

    def __init__(self, spec: TeacherSpec):
        self.spec = spec

    async def _complete(self, system: str, user: str) -> tuple[str, dict]:   # pragma: no cover - 子类实现
        raise NotImplementedError

    async def answer(self, state, questions: dict) -> TeacherAnswer:
        t0 = time.perf_counter()
        prompt = render_prompt(state, questions)
        last = ""
        for attempt in range(self.spec.retries):
            try:
                text, usage = await self._complete(_SYSTEM, prompt)
                answers = parse_answers(text, questions)
                if answers is None:
                    last = f"unparsable: {text[:160]}"
                    continue
                return TeacherAnswer(answers, usage, time.perf_counter() - t0)
            except Exception as e:
                last = f"{type(e).__name__}: {e}"
                if attempt < self.spec.retries - 1:
                    await asyncio.sleep(min(60, 5 * 2 ** attempt))  # 最后一次失败直接返回
        return TeacherAnswer(None, {}, time.perf_counter() - t0, last)


class OpenAITeacher(_LLMTeacher):
    """OpenAI Chat Completions 协议。"""

    async def _complete(self, system: str, user: str) -> tuple[str, dict]:
        body = {"model": self.spec.model, "max_tokens": self.spec.max_tokens,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                **self.spec.extra}
        if self.spec.temperature is not None:
            body.setdefault("temperature", self.spec.temperature)
        data = await _post(self.spec, f"{self.spec.url.rstrip('/')}/chat/completions",
                           {"Authorization": f"Bearer {_key(self.spec)}", "Content-Type": "application/json"}, body)
        return data["choices"][0]["message"].get("content") or "", data.get("usage") or {}


class AnthropicTeacher(_LLMTeacher):
    """Anthropic Messages 协议(Anthropic 官方或兼容地址)。思考块(thinking)忽略,只取 text 块。"""

    def _url(self) -> str:
        from app.llm_client import anthropic_url
        return anthropic_url(self.spec.url)

    async def _complete(self, system: str, user: str) -> tuple[str, dict]:
        body = {"model": self.spec.model, "max_tokens": self.spec.max_tokens, "system": system,
                "messages": [{"role": "user", "content": user}], **self.spec.extra}
        if self.spec.temperature is not None and "thinking" not in self.spec.extra:
            body.setdefault("temperature", self.spec.temperature)   # 开思考时 Claude 只接受默认温度
        data = await _post(self.spec, self._url(),
                           {"x-api-key": _key(self.spec), "anthropic-version": self.spec.api_version,
                            "Content-Type": "application/json"}, body)
        text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        u = data.get("usage") or {}
        usage = {**u, "total_tokens": int(u.get("input_tokens") or 0) + int(u.get("output_tokens") or 0)}
        return text, usage


# ── 注册表 ─────────────────────────────────────────────────────

def default_specs() -> list[TeacherSpec]:
    """内置 jev:当前绑定的适用性判官(后台「模型绑定」),未绑定时读 TYPESAFE_*。"""
    from app import model_registry, settings
    hit = model_registry.current().resolve(settings.get("model.judge"), ("judge",))
    if hit:
        m, p = hit
        return [TeacherSpec(name=model_registry.JUDGE_ALIAS, kind="system_one", url=p.base_url, model=m.model,
                            api_key=p.api_key, headers=dict(p.headers), timeout=60, concurrency=4)]
    return [TeacherSpec(name="jev", kind="system_one",
                        url=os.environ.get("TYPESAFE_API_URL", "https://api.typesafe.ai/v1"),
                        model=os.environ.get("TYPESAFE_MODEL", "jev-1.13.0"),
                        key_env="TYPESAFE_API_KEY", timeout=60, concurrency=4)]


def _specs_from(raw_list) -> list[TeacherSpec]:
    if not isinstance(raw_list, list):
        raise ValueError("teacher config must be a JSON list of specs")
    specs = [TeacherSpec(**raw) for raw in raw_list]
    for s in specs:
        if s.kind not in PROTOCOLS:
            raise ValueError(f"teacher {s.name}: unknown kind {s.kind!r}")
    return specs


def env_specs(path: str | None = None) -> list[TeacherSpec]:
    """旧写法的老师:CE_TEACHERS_FILE / CE_TEACHERS(后者覆盖同名项),密钥从 key_env 解析进 api_key。"""
    specs: dict[str, TeacherSpec] = {}
    path = path or os.environ.get("CE_TEACHERS_FILE", "")
    if path:
        for spec in _specs_from(json.loads(Path(path).read_text(encoding="utf-8"))):
            specs[spec.name] = spec
    inline = os.environ.get("CE_TEACHERS", "").strip()
    if inline:
        for spec in _specs_from(json.loads(inline)):
            specs[spec.name] = spec
    return [replace(spec, api_key=_key(spec)) for spec in specs.values()]


def db_specs() -> list[TeacherSpec]:
    """后台「模型」页里的对话模型与判官模型,按注册名成为老师。"""
    from app import model_registry
    reg = model_registry.current()
    out = []
    for m in reg.models.values():
        hit = reg.resolve(m.id, ("chat", "judge"))
        if hit is None:
            continue
        _, p = hit
        prm = m.params
        if m.kind == "judge":
            out.append(TeacherSpec(name=m.id, kind="system_one", url=p.base_url, model=m.model, api_key=p.api_key,
                                   headers=dict(p.headers), timeout=float(prm.get("timeout", 60)),
                                   concurrency=int(prm.get("concurrency", 4)), retries=int(prm.get("retries", 3))))
        else:
            out.append(TeacherSpec(
                name=m.id, kind=p.protocol, url=p.base_url, model=m.model, api_key=p.api_key,
                extra=dict(prm.get("extra") or {}), headers=dict(p.headers),
                temperature=prm["temperature"] if "temperature" in prm else 0.0,
                max_tokens=int(prm.get("max_tokens", 16000)), timeout=float(prm.get("timeout", 300)),
                concurrency=int(prm.get("concurrency", 2)), retries=int(prm.get("retries", 3)),
                api_version=str(prm.get("api_version", "2023-06-01"))))
    return out


def load_specs(path: str | None = None) -> dict[str, TeacherSpec]:
    specs = {s.name: s for s in default_specs()}
    for spec in env_specs(path):
        specs[spec.name] = spec
    for spec in db_specs():
        specs[spec.name] = spec
    return specs


def make_teacher(spec: TeacherSpec):
    if spec.kind == "system_one":
        return SystemOneTeacher(spec)
    if spec.kind == "openai":
        return OpenAITeacher(spec)
    if spec.kind == "anthropic":
        return AnthropicTeacher(spec)
    raise ValueError(f"unknown teacher kind: {spec.kind}")


def load_teachers(names: list[str] | None = None, path: str | None = None) -> dict:
    specs = load_specs(path)
    missing = [n for n in (names or []) if n not in specs]
    if missing:
        raise KeyError(f"unknown teachers: {missing}; known: {sorted(specs)}")
    return {n: make_teacher(specs[n]) for n in (names or list(specs))}


_CACHE: dict = {}


def _clear_cache() -> None:
    _CACHE.clear()


def _register_listener() -> None:
    from app import model_registry
    model_registry.on_change(_clear_cache)   # 后台改了模型 → 下次按新配置实例化


_register_listener()


def get_teacher(name: str):
    """按注册表名字取老师(进程内缓存)。"""
    if name not in _CACHE:
        _CACHE[name] = load_teachers([name])[name]
    return _CACHE[name]


async def ask(teacher: str | TeacherSpec, state, questions: dict) -> TeacherAnswer:
    """一句话调用:注册表名字或一条 TeacherSpec。"""
    t = make_teacher(teacher) if isinstance(teacher, TeacherSpec) else get_teacher(teacher)
    return await t.answer(state, questions)

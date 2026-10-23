"""Gardener router — 从 WebUI 操作 headless Claude Code 对酶执行六阶段循环。

MonkeyCode「云端终端与任务执行」的单用户缩尺版:WebUI 是任务工作台,
本路由负责拼装运行环境(env 注入模型端点/令牌,借鉴其 claude.tmpl 思路)、
启动一次性 CC 进程(宿主机 CLI 或 docker 容器)、收集日志、生成外部调度命令。

docker 模式经 **Docker Engine HTTP API** 直连(本机 socket / tcp://远程主机,
docker_host 可配),不需要本机 docker CLI;提示词作为命令行参数传入容器
(Engine API 免劫持喂不了 stdin),凭证仍走容器 Env。

架构红线(docs/gardener_design.md):
- 园丁产出只能经 CE API(report 带证据 / harvest 试用期置信度)回写;
- docker 模式仓库只读挂载;两种模式凭证都走环境变量,不进提示词与日志;
- 单次运行有界:条数上限进提示词,总超时强制杀进程;
- 同一时刻只允许一个园丁在跑(409)。
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, HTTPException

from app.core.dockerhost import (
    ContainerRunError,
    client_for,
    remove_container,
    run_container_stream,
)
from app import model_registry
from app.core.engine import Engine
from app.dependencies import get_engine
from app.routers._helpers import audit
from app.routers.auth import require_admin

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/gardener", tags=["gardener"], dependencies=[Depends(require_admin)])

_SETTING_KEY = "gardener_config"
_PROMPT_PATH = Path(__file__).resolve().parent.parent.parent / "ops" / "gardener" / "gardener.md"
_MASK = "••••••••"
_SECRET_KEYS = {"auth_token", "api_token"}

_DEFAULTS: dict = {
    "mode": "docker",         # docker: 一次性容器(部署形态) / local: 宿主机 claude CLI
    "model_id": "",           # 模型库里的 anthropic 协议对话模型;选了就用它的地址 / 密钥 / 模型名,下面三项作废
    "model": "",              # 留空用 CC 默认;夜间批建议配便宜模型
    "base_url": "",           # ANTHROPIC_BASE_URL(任意兼容端点,留空走官方)
    "auth_token": "",         # ANTHROPIC_AUTH_TOKEN
    "repo_path": "",          # 目标仓库:docker 模式=**docker 宿主机**上的路径(只读挂载),local 模式=cwd
    "api_url": "",            # 园丁访问 CE 的地址,留空按模式取默认
    "api_token": "",          # CE 静态机器令牌(园丁的回写身份)
    "max_enzymes": 20,
    "timeout_minutes": 30,
    "allowed_tools": "Read,Grep,Glob,Bash(curl:*)",
    "docker_image": "ce-gardener",
    "docker_host": "",         # Engine API 地址:留空=本机 socket;tcp://ip:2375=远程主机
    "nightly_enabled": False,  # 服务端定时:每晚自动夜巡
    "nightly_hour": 3,         # 服务器本地时区的整点(0-23)
}

_CONTAINER_NAME = "ce-gardener-run"  # 固定容器名:同时只跑一个,残留可清

_LAST_RUN_KEY = "gardener_last_run"  # 上次夜巡结果(持久化,重启后页面仍可见夜报)
_LOG_TAIL_LINES = 200                # 夜报只存日志尾部——六阶段收尾总结在末尾


# ── 运行状态(单例,同一时刻只跑一个园丁) ─────────────────────

class GardenerState:
    def __init__(self):
        self.running = False
        self.started_at: str | None = None
        self.finished_at: str | None = None
        self.exit_code: int | None = None
        self.error: str = ""
        self.trigger: str = ""              # 本次运行触发方式(manual/nightly)
        self.stop_requested: bool = False   # 用户主动停止——区分「手动停止」与「真实失败」的唯一语境
        self.log: deque[str] = deque(maxlen=2000)
        self.log_total: int = 0             # 累计日志行数:deque 丢头后仍单调递增(绝对行号基准)
        self.proc: asyncio.subprocess.Process | None = None
        self.task: asyncio.Task | None = None
        self.docker_host: str | None = None  # docker 模式运行中:供 stop 删远端容器

    def append_log(self, line: str) -> None:
        self.log.append(line)
        self.log_total += 1


state = GardenerState()


# ── 配置 ───────────────────────────────────────────────────────

async def _load_config(pg) -> dict:
    cfg = dict(_DEFAULTS)
    raw = await pg.get_setting(_SETTING_KEY, "")
    if raw:
        try:
            cfg.update({k: v for k, v in json.loads(raw).items() if k in _DEFAULTS})
        except (json.JSONDecodeError, TypeError):
            logger.warning("Ignoring corrupt %s setting", _SETTING_KEY)
    cfg["max_enzymes"] = max(1, min(50, int(cfg.get("max_enzymes") or 20)))
    cfg["timeout_minutes"] = max(5, min(120, int(cfg.get("timeout_minutes") or 30)))
    cfg["nightly_hour"] = max(0, min(23, int(cfg.get("nightly_hour") or 0)))
    cfg["nightly_enabled"] = bool(cfg.get("nightly_enabled"))
    return cfg


def _masked(cfg: dict) -> dict:
    out = dict(cfg)
    for key in _SECRET_KEYS:
        out[key] = _MASK if cfg.get(key) else ""
    return out


def _is_dir(path: str) -> bool:
    """Path.is_dir 的安全版:Windows 上不可达盘符会抛 OSError 而非返回 False。"""
    try:
        return Path(path).is_dir()
    except OSError:
        return False


def _resolve_api_url(cfg: dict) -> str:
    if cfg.get("api_url"):
        return str(cfg["api_url"]).rstrip("/")
    # docker 内访问宿主机;local 直连本机
    from app.config import API_PORT
    return (f"http://host.docker.internal:{API_PORT}" if cfg["mode"] == "docker"
            else f"http://127.0.0.1:{API_PORT}")


def _build_prompt(cfg: dict) -> str:
    if not _PROMPT_PATH.exists():
        raise HTTPException(500, f"缺少提示词文件: {_PROMPT_PATH}")
    manual = _PROMPT_PATH.read_text(encoding="utf-8")
    return (manual
            .replace("{{API_URL}}", _resolve_api_url(cfg))
            .replace("{{MAX_ENZYMES}}", str(cfg["max_enzymes"])))


def _model_endpoint(cfg: dict) -> tuple[str, str, str]:
    """(ANTHROPIC_BASE_URL, ANTHROPIC_AUTH_TOKEN, ANTHROPIC_MODEL):选了模型库里的 anthropic 协议模型
    就用它(服务商地址去掉 /v1 后缀——Claude Code 自己补),否则用园丁配置里手填的三项。"""
    if cfg.get("model_id"):
        hit = model_registry.current().resolve(cfg["model_id"], ("chat",))
        if hit and hit[1].protocol == "anthropic":
            m, p = hit
            base = p.base_url[:-3] if p.base_url.endswith("/v1") else p.base_url
            return base, p.api_key, m.model
    return cfg.get("base_url") or "", cfg.get("auth_token") or "", cfg.get("model") or ""


def _model_env(cfg: dict) -> dict:
    extra_env = {"CE_API_TOKEN": cfg.get("api_token") or ""}
    base_url, auth_token, model = _model_endpoint(cfg)
    if base_url:
        extra_env["ANTHROPIC_BASE_URL"] = base_url
    if auth_token:
        extra_env["ANTHROPIC_AUTH_TOKEN"] = auth_token
    if model:
        extra_env["ANTHROPIC_MODEL"] = model
    return extra_env


def _claude_args(cfg: dict) -> list[str]:
    return ["-p", "--output-format", "text", "--allowedTools", cfg["allowed_tools"]]


def _build_invocation(cfg: dict) -> tuple[list[str], dict, str | None]:
    """local 模式:宿主机 claude CLI 的 (argv, 额外env, cwd)。凭证走 env 不进命令行。"""
    claude = shutil.which("claude")
    if not claude:
        raise HTTPException(400, "宿主机没有 claude CLI(或不在 PATH)。请 npm i -g @anthropic-ai/claude-code,或改用 docker 模式。")
    cwd = cfg.get("repo_path") or None
    if cwd and not _is_dir(cwd):
        raise HTTPException(400, f"repo_path 不存在: {cwd}")
    return [claude, *_claude_args(cfg)], _model_env(cfg), cwd


def _build_docker_spec(cfg: dict, prompt: str) -> dict:
    """docker 模式:Engine API 一次性容器的运行规格(run_container_stream 入参)。

    仓库只读挂载物理隔离;提示词作为命令行参数;host.docker.internal 指向
    docker 宿主机网关,供容器回写 CE API(后端与 docker 主机不同机时需在
    api_url 里显式填可达地址)。
    """
    binds = []
    if cfg.get("repo_path"):
        binds.append(f"{cfg['repo_path']}:/workspace:ro")
    return {
        "docker_host": cfg.get("docker_host") or "",
        "image": cfg.get("docker_image") or "ce-gardener",
        "cmd": [*_claude_args(cfg), prompt],
        "env": _model_env(cfg),
        "binds": binds,
        "name": _CONTAINER_NAME,
        "extra_hosts": ["host.docker.internal:host-gateway"],
    }


def _external_command(cfg: dict) -> str:
    """给计划任务/手动运行的可复制命令(令牌以占位符出现,绝不外泄)。"""
    tools = cfg["allowed_tools"]
    api_url = _resolve_api_url(cfg)
    base_url, _, model_name = _model_endpoint(cfg)
    if cfg["mode"] == "docker":
        host_flag = f" -H {cfg['docker_host']}" if cfg.get("docker_host") else ""
        mount = f" -v {cfg['repo_path']}:/workspace:ro" if cfg.get("repo_path") else ""
        model = f" -e ANTHROPIC_MODEL={model_name}" if model_name else ""
        base = f" -e ANTHROPIC_BASE_URL={base_url}" if base_url else ""
        return (f"type ops\\gardener\\gardener.md | docker{host_flag} run --rm -i"
                f" --add-host=host.docker.internal:host-gateway{mount}"
                f"{base}{model} -e ANTHROPIC_AUTH_TOKEN=<模型令牌> -e CE_API_TOKEN=<CE令牌>"
                f" {cfg.get('docker_image') or 'ce-gardener'}"
                f" -p --output-format text --allowedTools \"{tools}\"")
    cd = f"cd {cfg['repo_path']}; " if cfg.get("repo_path") else ""
    return (f"$env:CE_API_TOKEN='<CE令牌>'; $env:ANTHROPIC_AUTH_TOKEN='<模型令牌>'; "
            f"{cd}type <CE仓库>\\ops\\gardener\\gardener.md | claude -p"
            f" --output-format text --allowedTools \"{tools}\""
            f"  # 提示词已内置 API 地址 {api_url}")


# ── 运行 ───────────────────────────────────────────────────────

# local 模式只继承运行 CLI 必需的基础变量。不能 `**os.environ`:
# 服务进程环境里有 CE_PG_PASSWORD / CE_JWT_SECRET 和全部 LLM/向量/判官 key,
# 而 agent 的 allowedTools 含 Bash —— 一条注入的仓库内容就能把密钥外传。
# (docker 模式本来就只传 _model_env,这里对齐。)
_INHERIT_ENV_KEYS = (
    "PATH", "HOME", "USERPROFILE", "SHELL", "LANG", "LC_ALL", "LC_CTYPE",
    "TMPDIR", "TEMP", "TMP", "SystemRoot", "COMSPEC", "APPDATA",
    "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "NPM_CONFIG_PREFIX", "NODE_PATH",
)


async def _run(argv: list[str], extra_env: dict, cwd: str | None,
               prompt: str, timeout_minutes: int) -> None:
    import os
    env = {k: os.environ[k] for k in _INHERIT_ENV_KEYS if k in os.environ}
    env.update({k: v for k, v in extra_env.items() if v})
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=cwd,
            env=env,
        )
        state.proc = proc
        proc.stdin.write(prompt.encode("utf-8"))
        await proc.stdin.drain()
        proc.stdin.close()

        async def pump():
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                state.append_log(line.decode("utf-8", errors="replace").rstrip())

        await asyncio.wait_for(asyncio.gather(pump(), proc.wait()),
                               timeout=timeout_minutes * 60)
        state.exit_code = proc.returncode
    except asyncio.TimeoutError:
        state.error = f"超时({timeout_minutes} 分钟),已强制终止"
        state.append_log(f"[gardener] {state.error}")
        _kill()
    except asyncio.CancelledError:
        _kill()
        raise
    except Exception as e:
        state.error = str(e)
        state.append_log(f"[gardener] 启动/运行失败: {e}")
        logger.exception("gardener run failed")
    finally:
        state.running = False
        state.proc = None
        state.finished_at = datetime.now(timezone.utc).isoformat()


async def _run_docker(spec: dict, timeout_minutes: int) -> None:
    """docker 模式:Engine API 跑一次性容器,输出进 state.log,超时强杀。"""
    result: dict = {}
    state.docker_host = spec["docker_host"]
    try:
        async with asyncio.timeout(timeout_minutes * 60):
            buf = ""
            async for chunk in run_container_stream(**spec, result=result):
                buf += chunk
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    state.append_log(line.rstrip())
            if buf.strip():
                state.append_log(buf.rstrip())
        state.exit_code = result.get("exit_code")
    except TimeoutError:
        state.error = f"超时({timeout_minutes} 分钟),已强制终止"
        state.append_log(f"[gardener] {state.error}")
        await remove_container(spec["docker_host"], spec["name"])
    except asyncio.CancelledError:
        await remove_container(spec["docker_host"], spec["name"])
        raise
    except (ContainerRunError, httpx.HTTPError) as e:
        state.error = str(e)
        state.append_log(f"[gardener] 容器启动/运行失败: {e}")
    except Exception as e:  # noqa: BLE001
        state.error = str(e)
        state.append_log(f"[gardener] 运行失败: {e}")
        logger.exception("gardener docker run failed")
    finally:
        state.running = False
        state.docker_host = None
        state.finished_at = datetime.now(timezone.utc).isoformat()


def _kill() -> None:
    if state.proc and state.proc.returncode is None:
        try:
            state.proc.kill()
        except ProcessLookupError:
            pass


def _status() -> dict:
    return {
        "running": state.running,
        "started_at": state.started_at,
        "finished_at": state.finished_at,
        "exit_code": state.exit_code,
        "error": state.error,
        "log_lines": state.log_total,  # 绝对累计行数(deque 丢头后仍单调递增)
    }


# ── 上次夜巡结果持久化 ─────────────────────────────────────────

def _last_run_snapshot() -> dict:
    """从 state 同步组装本次运行结果快照(持久化与 /log 完成响应共用同一 shape)。"""
    return {
        "started_at": state.started_at,
        "finished_at": state.finished_at,
        "exit_code": state.exit_code,
        "error": state.error,
        "trigger": state.trigger,
        "stopped": state.stop_requested,
        "log_tail": list(state.log)[-_LOG_TAIL_LINES:],
    }


async def _supervise(runner, pg) -> None:
    """包装运行协程:收尾后把本次结果持久化为 gardener_last_run。

    _run/_run_docker 是模块级函数拿不到 pg 句柄,而 start_run 创建 Task 时
    手里有 engine——在这里统一包一层,local/docker 两种模式、manual/nightly
    两种触发共用同一段收尾代码。持久化失败只告警,绝不影响运行状态收尾
    (state 已在 runner 自己的 finally 里归位)。
    """
    try:
        await runner
    finally:
        # 快照必须在首个 await 之前同步构建——隐式约束:runner 的 finally 里
        # 不得出现 await(runner 归位 running=False 后一旦让出事件循环,
        # 新 run 可能插入并重置 state,快照就会串成别人的数据)。
        snapshot = _last_run_snapshot()
        try:
            await pg.set_setting(_LAST_RUN_KEY,
                                 json.dumps(snapshot, ensure_ascii=False))
        except Exception:  # noqa: BLE001 — 夜报存不上不能连累运行收尾
            logger.warning("gardener last_run 持久化失败(不影响运行收尾)",
                           exc_info=True)


async def _load_last_run(pg) -> dict | None:
    """读上次夜巡结果;无记录或 JSON 损坏一律容错返回 None。

    归一化 shape 而非透传任意 dict:缺 log_tail/trigger/stopped 的旧记录
    也补齐字段,保证前端契约完整。
    """
    raw = await pg.get_setting(_LAST_RUN_KEY, "")
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("Ignoring corrupt %s setting", _LAST_RUN_KEY)
        return None
    if not isinstance(data, dict):
        logger.warning("Ignoring corrupt %s setting", _LAST_RUN_KEY)
        return None
    tail = data.get("log_tail")
    log_tail = [str(x) for x in tail] if isinstance(tail, list) else []
    return {
        "started_at": data.get("started_at"),
        "finished_at": data.get("finished_at"),
        "exit_code": data.get("exit_code"),
        "error": str(data.get("error") or ""),
        "trigger": str(data.get("trigger") or ""),
        "stopped": bool(data.get("stopped")),
        "log_tail": log_tail,
    }


# ── 端点 ───────────────────────────────────────────────────────

@router.get("")
async def get_gardener(engine: Engine = Depends(get_engine)):
    cfg, last_run = await asyncio.gather(
        _load_config(engine.pg), _load_last_run(engine.pg))
    return {
        "config": _masked(cfg),
        "status": _status(),
        "last_run": last_run,
        "command": _external_command(cfg),
        "prompt_exists": _PROMPT_PATH.exists(),
    }


@router.put("/config")
async def update_config(body: dict, engine: Engine = Depends(get_engine),
                        user: dict = Depends(require_admin)):
    """只给管理员:这里决定园丁用哪个模型端点、连哪台 docker 主机、挂哪个仓库。"""
    cfg = await _load_config(engine.pg)
    for key in _DEFAULTS:
        if key not in body:
            continue
        value = body[key]
        if key in _SECRET_KEYS and value == _MASK:
            continue  # 回显的掩码 → 保留原值
        if key in ("max_enzymes", "timeout_minutes"):
            try:
                value = int(value)
            except (TypeError, ValueError):
                raise HTTPException(400, f"{key} 必须是整数") from None
        cfg[key] = value
    if cfg["mode"] not in ("local", "docker"):
        raise HTTPException(400, "mode 只能是 local 或 docker")
    dh = str(cfg.get("docker_host") or "")
    if dh and not dh.startswith(("unix://", "tcp://", "http://", "https://")):
        raise HTTPException(400, "docker_host 只支持留空 / unix:// / tcp:// / http(s)://")
    if cfg.get("model_id"):
        hit = model_registry.current().resolve(cfg["model_id"], ("chat",))
        if not hit or hit[1].protocol != "anthropic":
            raise HTTPException(400, "园丁只能使用 anthropic 协议的对话模型(Claude Code 走 Messages API)")
    cfg["max_enzymes"] = max(1, min(50, cfg["max_enzymes"]))
    cfg["timeout_minutes"] = max(5, min(120, cfg["timeout_minutes"]))
    await engine.pg.set_setting(_SETTING_KEY, json.dumps(cfg, ensure_ascii=False))
    await audit(engine, user, "gardener.config", "", {k: v for k, v in body.items() if k not in _SECRET_KEYS})
    return {"config": _masked(cfg), "command": _external_command(cfg),
            "message": "园丁配置已保存"}


async def _effective_api_token(pg, cfg: dict) -> str:
    """园丁的 CE 回写令牌:显式配置优先,留空回退本机静态机器令牌。"""
    if cfg.get("api_token"):
        return cfg["api_token"]
    return await pg.get_setting("api_auth_token", "") or ""


@router.get("/check")
async def check_environment(engine: Engine = Depends(get_engine)):
    """环境自检:把「能不能跑」拆成可指认的单项,别让用户靠报错猜。"""
    cfg = await _load_config(engine.pg)
    api_token = await _effective_api_token(engine.pg, cfg)
    repo = cfg.get("repo_path") or ""
    base_url, auth_token, model_name = _model_endpoint(cfg)
    model_missing = bool(cfg.get("model_id")) and not model_registry.current().resolve(cfg["model_id"], ("chat",))
    if cfg["mode"] == "docker":
        addr = cfg.get("docker_host") or ""
        label = addr or "本机 socket"
        try:
            async with client_for(addr) as client:
                resp = await client.get("/_ping", timeout=3.0)
            runner_ok = resp.status_code == 200
            runner_detail = (f"docker 主机可达({label})" if runner_ok
                             else f"docker 主机响应异常({label}): HTTP {resp.status_code}")
        except Exception as e:  # noqa: BLE001 — UDS 在 Windows 上可能抛非 httpx 异常
            runner_ok, runner_detail = False, f"连不上 docker 主机({label}): {e}"
    else:
        claude = shutil.which("claude")
        runner_ok = bool(claude)
        runner_detail = claude or "未找到 claude CLI,请 npm i -g @anthropic-ai/claude-code"
    checks = [
        {
            "key": "runner",
            "label": f"运行器({cfg['mode']} 模式)",
            "ok": runner_ok,
            "detail": runner_detail,
        },
        {
            "key": "prompt",
            "label": "六阶段手册 (ops/gardener/gardener.md)",
            "ok": _PROMPT_PATH.exists(),
            "detail": str(_PROMPT_PATH),
        },
        {
            "key": "model",
            "label": "模型端点",
            "ok": not model_missing and (bool(auth_token) or not base_url),
            "detail": ("选中的模型不存在、已停用或不是 anthropic 协议" if model_missing else
                       f"{base_url or 'Anthropic 官方'} / 模型 {model_name or '(CC 默认)'}"
                       + ("" if auth_token or not base_url else " — 配了第三方端点但没配令牌")),
        },
        {
            "key": "ce_token",
            "label": "CE 回写令牌 (CE_API_TOKEN)",
            "ok": bool(api_token),
            "detail": ("已配置(园丁专用)" if cfg.get("api_token")
                       else ("回退本机静态机器令牌" if api_token
                             else "未配置——设置页生成静态 API 令牌,或在此页填写")),
        },
        {
            "key": "repo",
            "label": "目标仓库",
            "ok": not repo or _is_dir(repo),
            "detail": (repo + ("" if _is_dir(repo) else " — 路径不存在")) if repo
                      else "未配置(园丁将无法做现实对账,仅能整理酶库)",
        },
    ]
    return {"ok": all(c["ok"] for c in checks), "checks": checks}


async def start_run(engine: Engine, trigger: str = "manual") -> None:
    """加载配置并启动一次夜巡。前置条件不满足时抛 HTTPException。

    手动端点与夜间定时器共用这一个入口,保证守卫逻辑只有一份。
    """
    if state.running:
        raise HTTPException(409, "园丁正在运行中")
    cfg = await _load_config(engine.pg)
    cfg["api_token"] = await _effective_api_token(engine.pg, cfg)
    if not cfg["api_token"]:
        raise HTTPException(400, "缺少 CE 回写令牌:在设置页生成静态 API 令牌,或在园丁配置里填写")
    prompt = _build_prompt(cfg)

    if cfg["mode"] == "docker":
        spec = _build_docker_spec(cfg, prompt)
        launch_note = (f"docker[{spec['docker_host'] or '本机 socket'}] "
                       f"{spec['image']} {' '.join(spec['cmd'][:-1])} <提示词>")
        runner = _run_docker(spec, cfg["timeout_minutes"])
    else:
        argv, extra_env, cwd = _build_invocation(cfg)
        launch_note = " ".join(argv)
        runner = _run(argv, extra_env, cwd, prompt, cfg["timeout_minutes"])

    state.running = True
    state.started_at = datetime.now(timezone.utc).isoformat()
    state.finished_at = None
    state.exit_code = None
    state.error = ""
    state.trigger = trigger
    state.stop_requested = False
    state.log.clear()
    state.log_total = 0
    state.append_log(f"[gardener] 启动({trigger}): {launch_note}")
    state.task = asyncio.create_task(_supervise(runner, engine.pg))


@router.post("/run")
async def run_gardener(engine: Engine = Depends(get_engine)):
    await start_run(engine, trigger="manual")
    return {"status": "started", "message": "园丁已出发,日志实时可查"}


# ── 夜间定时(服务端调度,不依赖宿主机计划任务) ────────────────

_LAST_NIGHT_KEY = "gardener_last_night"


def nightly_due(cfg: dict, now: datetime, last_date: str) -> bool:
    """到点判定:启用中、正处配置的整点、当天还没跑过。纯函数便于测试。"""
    if not cfg.get("nightly_enabled"):
        return False
    return now.hour == int(cfg.get("nightly_hour", 3)) and now.strftime("%Y-%m-%d") != last_date


async def gardener_loop(engine: Engine) -> None:
    """后台循环:每分钟检查一次是否到了夜巡时刻。

    先写 last_night 再启动——启动失败宁可当晚空跑,也不在整点内反复重试
    烧模型额度;失败原因落在 state.error,页面上看得到。
    """
    await asyncio.sleep(120)  # 等服务完全就绪
    while True:
        try:
            cfg = await _load_config(engine.pg)
            now = datetime.now()  # 服务器本地时区,与 UI 的"整点"语义一致
            last = await engine.pg.get_setting(_LAST_NIGHT_KEY, "") or ""
            if not state.running and nightly_due(cfg, now, last):
                await engine.pg.set_setting(_LAST_NIGHT_KEY, now.strftime("%Y-%m-%d"))
                try:
                    await start_run(engine, trigger="nightly")
                    logger.info("gardener nightly run started")
                except HTTPException as e:
                    state.error = f"定时夜巡未能启动: {e.detail}"
                    logger.warning("gardener nightly skipped: %s", e.detail)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("gardener loop error (non-fatal)")
        await asyncio.sleep(60)


@router.post("/stop")
async def stop_gardener():
    if not state.running:
        return {"status": "idle", "message": "没有运行中的园丁"}
    # 先立语境再动手:这是「用户主动停止」而非真实失败的唯一可判定依据
    state.stop_requested = True
    _kill()
    if state.docker_host is not None:
        # 删远端容器,日志流即断,_run_docker 自然收尾
        await remove_container(state.docker_host, _CONTAINER_NAME)
    return {"status": "stopping", "message": "已发送终止信号"}


@router.get("/log")
async def get_log(offset: int = 0):
    # offset 是绝对行号(基准 log_total,含 deque 已丢弃的头部):按 dropped
    # 折算成 deque 内相对下标,超 2000 行后前端增量拉取才不会错位冻结。
    dropped = state.log_total - len(state.log)
    rel = max(0, offset - dropped)
    lines = list(state.log)[rel:]
    # 运行结束后把夜报快照随完成信号同响应原子送达,前端不再赛跑 DB 持久化
    finished = not state.running and state.finished_at
    return {
        **_status(),
        "offset": offset,
        "lines": lines,
        "last_run": _last_run_snapshot() if finished else None,
    }

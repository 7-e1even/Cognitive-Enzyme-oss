"""园丁路由单测 — FakePG + 真实子进程,无需连库。

直接跑: python tests/test_gardener.py
覆盖:配置读写与掩码语义、提示词占位符、argv 拼装(凭证不进命令行)、
     令牌回退、环境自检、运行守卫(409/400)、_run 真实子进程(stdin 注入/
     日志泵/退出码)与超时强杀、claude CLI 可拉起性。
"""
import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import HTTPException  # noqa: E402

from app.routers import gardener  # noqa: E402


class FakePG:
    def __init__(self, settings=None):
        self.settings = dict(settings or {})

    async def get_setting(self, key, default=None):
        return self.settings.get(key, default)

    async def set_setting(self, key, value):
        self.settings[key] = value


class FakeEngine:
    def __init__(self, settings=None):
        self.pg = FakePG(settings)


def _reset_state():
    gardener.state = gardener.GardenerState()


async def test_config_roundtrip_and_masking():
    eng = FakeEngine()
    res = await gardener.update_config(
        {"mode": "local", "auth_token": "sk-secret", "api_token": "ce-secret",
         "model": "glm-4.6", "max_enzymes": 99},
        engine=eng,
    )
    assert res["config"]["auth_token"] == gardener._MASK      # 秘密只回显掩码
    assert res["config"]["model"] == "glm-4.6"
    assert res["config"]["max_enzymes"] == 50                 # 上限夹紧
    stored = json.loads(eng.pg.settings["gardener_config"])
    assert stored["auth_token"] == "sk-secret"                # 库里存真值

    # 回传掩码 → 保留原值;传新值 → 覆盖
    await gardener.update_config(
        {"auth_token": gardener._MASK, "api_token": "ce-new"}, engine=eng)
    stored = json.loads(eng.pg.settings["gardener_config"])
    assert stored["auth_token"] == "sk-secret"
    assert stored["api_token"] == "ce-new"
    print("[ok] config_roundtrip_and_masking")


async def test_prompt_placeholders():
    eng = FakeEngine({"gardener_config": json.dumps(
        {"mode": "local", "max_enzymes": 7})})
    cfg = await gardener._load_config(eng.pg)
    prompt = gardener._build_prompt(cfg)
    assert "{{API_URL}}" not in prompt and "{{MAX_ENZYMES}}" not in prompt
    assert "http://127.0.0.1:8420" in prompt                  # local 默认地址
    assert "≤7 条" in prompt or "7 条" in prompt
    print("[ok] prompt_placeholders")


async def test_invocation_shapes():
    eng = FakeEngine({"gardener_config": json.dumps({
        "mode": "local", "auth_token": "sk-x", "api_token": "ce-x",
        "base_url": "https://ep", "model": "m1"})})
    cfg = await gardener._load_config(eng.pg)
    # 只校验命令行与环境变量的拼法,不依赖本机装没装 claude CLI(CI 和服务器上通常没有)
    with patch.object(gardener.shutil, "which", lambda name: f"/usr/local/bin/{name}"):
        argv, env, cwd = gardener._build_invocation(cfg)
    assert argv[0].lower().endswith(("claude", "claude.exe", "claude.cmd")), argv[0]
    assert "-p" in argv and "--allowedTools" in argv
    assert all("sk-x" not in a and "ce-x" not in a for a in argv)  # 凭证不进命令行
    assert env["ANTHROPIC_AUTH_TOKEN"] == "sk-x" and env["CE_API_TOKEN"] == "ce-x"
    assert env["ANTHROPIC_BASE_URL"] == "https://ep" and env["ANTHROPIC_MODEL"] == "m1"

    # docker 模式:Engine API 运行规格(不依赖本机 docker CLI)
    cfg["mode"] = "docker"
    cfg["repo_path"] = "/srv/repos/demo"
    cfg["docker_host"] = "tcp://1.2.3.4:2375"
    spec = gardener._build_docker_spec(cfg, prompt="夜巡手册全文")
    assert spec["docker_host"] == "tcp://1.2.3.4:2375"
    assert spec["image"] == "ce-gardener" and spec["name"] == gardener._CONTAINER_NAME
    assert "/srv/repos/demo:/workspace:ro" in spec["binds"]    # 只读挂载
    assert spec["env"]["ANTHROPIC_AUTH_TOKEN"] == "sk-x"       # 凭证走容器 Env
    assert spec["env"]["CE_API_TOKEN"] == "ce-x"
    assert spec["cmd"][-1] == "夜巡手册全文"                    # 提示词作为位置参数
    assert "--allowedTools" in spec["cmd"]
    assert "host.docker.internal:host-gateway" in spec["extra_hosts"]
    print("[ok] invocation_shapes")


async def test_token_fallback():
    eng = FakeEngine({"api_auth_token": "static-tok"})
    cfg = await gardener._load_config(eng.pg)
    assert await gardener._effective_api_token(eng.pg, cfg) == "static-tok"
    cfg["api_token"] = "own-tok"
    assert await gardener._effective_api_token(eng.pg, cfg) == "own-tok"
    print("[ok] token_fallback")


async def test_check_endpoint():
    eng = FakeEngine({"gardener_config": json.dumps(
        {"mode": "local", "repo_path": "Z:\\no\\such\\dir",
         "base_url": "https://ep"})})           # 第三方端点但没令牌
    res = await gardener.check_environment(engine=eng)
    by_key = {c["key"]: c for c in res["checks"]}
    assert not res["ok"]
    assert not by_key["repo"]["ok"]                            # 路径不存在
    assert not by_key["model"]["ok"]                           # 端点缺令牌
    assert not by_key["ce_token"]["ok"]                        # 无任何令牌
    assert by_key["prompt"]["ok"]                              # 手册在仓库里
    print("[ok] check_endpoint")


async def test_run_guards():
    _reset_state()
    gardener.state.running = True
    try:
        await gardener.run_gardener(engine=FakeEngine())
        raise AssertionError("应 409")
    except HTTPException as e:
        assert e.status_code == 409
    _reset_state()
    try:
        await gardener.run_gardener(engine=FakeEngine())       # 无任何令牌
        raise AssertionError("应 400")
    except HTTPException as e:
        assert e.status_code == 400 and "令牌" in e.detail
    print("[ok] run_guards")


async def test_real_subprocess_run():
    """用 python 顶替 claude,验证 stdin 注入/日志泵/退出码是真的能跑。"""
    _reset_state()
    gardener.state.running = True
    script = "import sys; d=sys.stdin.read(); print('GOT', len(d)); print('BYE')"
    await gardener._run([sys.executable, "-c", script], {}, None,
                        prompt="x" * 1234, timeout_minutes=1)
    lines = list(gardener.state.log)
    assert gardener.state.exit_code == 0, (gardener.state.exit_code, lines)
    assert any("GOT 1234" in l for l in lines), lines          # 提示词完整到达 stdin
    assert any("BYE" in l for l in lines)
    assert gardener.state.running is False and gardener.state.finished_at
    print("[ok] real_subprocess_run")


async def test_timeout_kill():
    _reset_state()
    gardener.state.running = True
    script = "import sys,time; sys.stdin.read(); print('start',flush=True); time.sleep(30)"
    await gardener._run([sys.executable, "-c", script], {}, None,
                        prompt="p", timeout_minutes=0.03)      # ≈1.8s
    assert "超时" in gardener.state.error, gardener.state.error
    assert gardener.state.running is False
    print("[ok] timeout_kill")


async def test_last_run_persisted():
    """运行收尾经 _supervise 持久化 gardener_last_run,log_tail 截断到 200 行。"""
    _reset_state()
    eng = FakeEngine()
    gardener.state.running = True
    gardener.state.started_at = "2026-07-07T00:00:00+00:00"
    gardener.state.trigger = "manual"          # start_run 设置;测试直接驱动 state
    script = ("import sys; sys.stdin.read(); "
              "[print(f'line{i}') for i in range(250)]")
    await gardener._supervise(
        gardener._run([sys.executable, "-c", script], {}, None,
                      prompt="p", timeout_minutes=1),
        eng.pg)
    data = json.loads(eng.pg.settings["gardener_last_run"])
    assert data["exit_code"] == 0 and data["trigger"] == "manual"
    assert data["stopped"] is False                            # 无人喊停
    assert data["started_at"] and data["finished_at"]
    assert len(data["log_tail"]) == 200                        # 只存最后 200 行
    assert data["log_tail"][-1] == "line249"                   # 尾部保留,头部截掉
    assert data["log_tail"][0] == "line50"

    # GET 端点回读同一条记录
    res = await gardener.get_gardener(engine=eng)
    assert res["last_run"]["exit_code"] == 0
    assert res["last_run"]["log_tail"][-1] == "line249"
    print("[ok] last_run_persisted")


async def test_last_run_stopped_flag_persisted():
    """stop 端点置 stop_requested → 快照带 stopped=True(手动停止≠真实失败)。"""
    _reset_state()
    eng = FakeEngine()
    gardener.state.running = True
    gardener.state.trigger = "manual"

    async def runner():
        # 模拟 stop 端点在运行期间被调用
        gardener.state.stop_requested = True
        gardener.state.running = False
        gardener.state.finished_at = "2026-07-07T00:01:00+00:00"

    await gardener._supervise(runner(), eng.pg)
    data = json.loads(eng.pg.settings["gardener_last_run"])
    assert data["stopped"] is True and data["trigger"] == "manual"
    print("[ok] last_run_stopped_flag_persisted")


async def test_last_run_absent_and_corrupt():
    """无记录 → last_run 为 None;JSON 损坏容错返回 None 而非炸掉端点。"""
    res = await gardener.get_gardener(engine=FakeEngine())
    assert res["last_run"] is None
    res = await gardener.get_gardener(
        engine=FakeEngine({"gardener_last_run": "{not json"}))
    assert res["last_run"] is None
    res = await gardener.get_gardener(
        engine=FakeEngine({"gardener_last_run": "[1,2]"}))     # 类型不对也容错
    assert res["last_run"] is None
    print("[ok] last_run_absent_and_corrupt")


async def test_last_run_normalized():
    """合法 dict 但缺 log_tail/trigger/stopped 的旧记录 → 归一化补齐而非透传。"""
    eng = FakeEngine({"gardener_last_run": json.dumps(
        {"exit_code": 1, "error": None,
         "started_at": "2026-07-06T19:00:00+00:00"})})
    res = await gardener.get_gardener(engine=eng)
    lr = res["last_run"]
    assert lr["log_tail"] == []                                # 缺字段不再让前端白屏
    assert lr["trigger"] == "" and lr["stopped"] is False
    assert lr["error"] == ""                                   # None 归一为空串
    assert lr["exit_code"] == 1
    assert lr["finished_at"] is None
    # log_tail 类型不对(非 list)同样归一为空列表
    eng2 = FakeEngine({"gardener_last_run": json.dumps(
        {"exit_code": 0, "log_tail": "oops"})})
    res2 = await gardener.get_gardener(engine=eng2)
    assert res2["last_run"]["log_tail"] == []
    print("[ok] last_run_normalized")


async def test_log_offset_with_dropped_head():
    """offset 是绝对行号:deque 丢头(log_total > len(log))后按 dropped 修正切片。"""
    _reset_state()
    st = gardener.state
    for i in range(2100):
        st.append_log(f"line{i}")
    assert st.log_total == 2100 and len(st.log) == 2000        # 头部 100 行已丢

    res = await gardener.get_log(offset=2050)
    assert res["log_lines"] == 2100                            # 计数用绝对累计
    assert len(res["lines"]) == 50 and res["lines"][0] == "line2050"

    # offset 落在已丢弃区间 → 从 deque 现存头部开始给,不越界也不冻结
    res = await gardener.get_log(offset=50)
    assert res["lines"][0] == "line100" and len(res["lines"]) == 2000

    # 追平后继续增量:无新行返回空
    res = await gardener.get_log(offset=2100)
    assert res["lines"] == []
    # 未在运行且无 finished_at → 不附带 last_run
    assert res["last_run"] is None

    # 运行结束后 /log 原子附带夜报快照(完成信号与夜报同响应)
    st.finished_at = "2026-07-07T00:02:00+00:00"
    st.exit_code = 0
    res = await gardener.get_log(offset=2100)
    assert res["last_run"] is not None
    assert res["last_run"]["exit_code"] == 0
    assert res["last_run"]["log_tail"][-1] == "line2099"
    _reset_state()
    print("[ok] log_offset_with_dropped_head")


async def test_last_run_persist_failure_swallowed():
    """持久化失败只告警,不能把异常抛回运行收尾。"""
    _reset_state()

    class BoomPG(FakePG):
        async def set_setting(self, key, value):
            raise RuntimeError("db down")

    async def runner():
        gardener.state.exit_code = 0

    await gardener._supervise(runner(), BoomPG())  # 不应抛
    print("[ok] last_run_persist_failure_swallowed")


def test_nightly_due():
    from datetime import datetime
    cfg = {"nightly_enabled": True, "nightly_hour": 3}
    at3 = datetime(2026, 7, 2, 3, 30)
    assert gardener.nightly_due(cfg, at3, "")                       # 到点且今天没跑
    assert not gardener.nightly_due(cfg, at3, "2026-07-02")         # 今天跑过
    assert gardener.nightly_due(cfg, at3, "2026-07-01")             # 昨天跑的
    assert not gardener.nightly_due(cfg, datetime(2026, 7, 2, 4, 0), "")   # 不在整点内
    assert not gardener.nightly_due({"nightly_enabled": False, "nightly_hour": 3}, at3, "")
    print("[ok] nightly_due")


async def test_claude_spawnable():
    """真实拉起 claude --version(不产生 API 调用),证明运行器路径可用。"""
    import shutil
    claude = shutil.which("claude")
    if not claude:
        print("[skip] claude_spawnable (未安装)")
        return
    proc = await asyncio.create_subprocess_exec(
        claude, "--version",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    out, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
    assert proc.returncode == 0, out.decode(errors="replace")
    print(f"[ok] claude_spawnable ({out.decode(errors='replace').strip()})")


async def main():
    print("=" * 50)
    print("园丁路由单测")
    print("=" * 50)
    await test_config_roundtrip_and_masking()
    await test_prompt_placeholders()
    await test_invocation_shapes()
    await test_token_fallback()
    await test_check_endpoint()
    await test_run_guards()
    await test_real_subprocess_run()
    await test_timeout_kill()
    await test_last_run_persisted()
    await test_last_run_stopped_flag_persisted()
    await test_last_run_absent_and_corrupt()
    await test_last_run_normalized()
    await test_log_offset_with_dropped_head()
    await test_last_run_persist_failure_swallowed()
    test_nightly_due()
    await test_claude_spawnable()
    print("=" * 50)
    print("All passed [PASS]")


if __name__ == "__main__":
    asyncio.run(main())

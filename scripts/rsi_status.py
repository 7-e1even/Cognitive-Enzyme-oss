#!/usr/bin/env python3
"""RSI 经验流进度速览。

用法:  .venv-eval/bin/python scripts/rsi_status.py            # 打一次
       .venv-eval/bin/python scripts/rsi_status.py -w [秒]     # 动态刷新,默认 30s,Ctrl-C 退出
       .venv-eval/bin/python scripts/rsi_status.py -w --plain  # 无 rich 时退回纯文本清屏刷新
"""
import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

D = Path(__file__).resolve().parent.parent / "tests/eval/hardbench/data/rsi"

# tag 目标 arm  (与 scripts/rsi-watchdog.sh 的 SPECS 保持一致)
SPECS = [
    ("phys", 372, "base"),
    ("phys", 372, "base2"),
    ("phys", 372, "self"),
    ("phys", 372, "self_v2"),
    ("phys", 372, "self_v3"),
    ("phys", 178, "reflect"),
    ("phys", 372, "self_hard"),
    ("phys", 372, "teacher"),
    ("phys", 372, "teacher_v3"),
    ("phys", 372, "teacher_hard"),
    ("phys", 372, "seed"),
    ("phys", 372, "seed_self"),
    ("math", 300, "base"),
    ("math", 300, "self"),
    ("math", 300, "teacher"),
    ("math", 300, "seed"),
    ("math", 300, "seed_self"),
]

ROW = re.compile(r"^(\d{2}):(\d{2}):(\d{2}) \[\w+\] +\[\d+\]")


def done_rows(path: Path) -> tuple[int, int, int]:
    """(完成数, 错误数, 答对数)"""
    done = err = correct = 0
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            if r.get("error"):
                err += 1
                continue
            done += 1
            correct += bool(r.get("correct"))
    except OSError:
        pass
    return done, err, correct


def recent_rate(log_path: Path, window: int = 15) -> float | None:
    """用 .log 里最近 window 条结果行的时间戳估算 题/小时。"""
    try:
        ts = [int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
              for line in log_path.read_text(encoding="utf-8").splitlines()[-window * 3:]
              if (m := ROW.match(line))]
    except OSError:
        return None
    if len(ts) < 3:
        return None
    ts = ts[-window:]
    # 跨午夜单调化
    out = [ts[0]]
    for t in ts[1:]:
        while t < out[-1]:
            t += 86400
        out.append(t)
    span = out[-1] - out[0]
    return (len(out) - 1) / (span / 3600) if span > 60 else None


def alive(tag: str, arm: str) -> bool:
    return subprocess.run(
        ["pgrep", "-f", f"tests.eval.hardbench.rsi --tag {tag} .*--arm {arm} --"],
        capture_output=True).returncode == 0


def bar(frac: float, width: int = 14) -> str:
    n = round(frac * width)
    return "█" * n + "░" * (width - n)


def ago(path: Path) -> str:
    try:
        m = int(time.time() - path.stat().st_mtime)
    except OSError:
        return "无文件"
    return f"{m // 60}分钟前" if m >= 60 else f"{m}秒前"


def render() -> str:
    total_done = total_target = 0
    rows = []
    for tag, target, arm in SPECS:
        done, err, correct = done_rows(D / f"{tag}.{arm}.jsonl")
        total_done += min(done, target)
        total_target += target
        if done == 0 and not (D / f"{tag}.{arm}.jsonl").exists():
            continue
        rate = recent_rate(D / f"{tag}.{arm}.log")
        state = "●运行" if alive(tag, arm) else "○停了"
        pct = done / target
        eta = f"剩约{(target - done) / rate:.1f}h" if rate and done < target else ""
        acc = f"{correct}/{done}" if done else ""
        rows.append(f"{tag}.{arm:<9} {done:>4}/{target} {bar(pct)} {pct:5.1%}  "
                    f"{state:<6} 正确率{acc:<9} 最近落行 {ago(D / f'{tag}.{arm}.jsonl'):<8} "
                    f"{f'{rate:.1f}题/h' if rate else '':<9} {eta}")

    wd = subprocess.run(["pgrep", "-f", "rsi-watchdog.sh"], capture_output=True).returncode == 0
    out = [f"RSI 经验流 · 总进度 {total_done}/{total_target} ({total_done / total_target:.1%})"
           f"  {time.strftime('%H:%M:%S')}\n",
           "\n".join(rows),
           f"\n看门狗 {'●在跑' if wd else '○没在跑'} · 日志 logs/rsi-*.out · 报表 {D}/{{phys,math}}.md"]
    return "\n".join(out)


def collect() -> list[dict]:
    rows = []
    for tag, target, arm in SPECS:
        done, err, correct = done_rows(D / f"{tag}.{arm}.jsonl")
        if done == 0 and not (D / f"{tag}.{arm}.jsonl").exists():
            continue
        rows.append({
            "name": f"{tag}.{arm}", "target": target, "done": done,
            "correct": correct, "rate": recent_rate(D / f"{tag}.{arm}.log"),
            "running": alive(tag, arm), "ago": ago(D / f"{tag}.{arm}.jsonl"),
        })
    return rows


def render_rich() -> "Group":
    from rich.console import Group
    from rich.panel import Panel
    from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn
    from rich.table import Table

    rows = collect()
    total_done = sum(min(r["done"], r["target"]) for r in rows)
    total_target = sum(r["target"] for r in rows)

    tp = Progress(TextColumn("总进度"), BarColumn(bar_width=None, style="dim", complete_style="green"),
                  TextColumn("{task.completed}/{task.total}"), TextColumn("{task.percentage:>5.1f}%"))
    tp.add_task("总进度", total=total_target, completed=total_done)

    grid = Table(expand=True, box=None, pad_edge=False, show_edge=False)
    grid.add_column("组", style="bold", no_wrap=True)
    grid.add_column("条", ratio=2)
    grid.add_column("比例", justify="right")
    grid.add_column("完成", justify="right")
    grid.add_column("正确率", justify="right")
    grid.add_column("落行", justify="right")
    grid.add_column("速度", justify="right")
    grid.add_column("预计", justify="right")

    for r in rows:
        bar = Progress(SpinnerColumn("dots"), BarColumn(bar_width=None, complete_style="cyan", finished_style="green"),
                       expand=True)
        t = bar.add_task("", total=r["target"], completed=r["done"])
        if not r["running"]:
            bar.stop_task(t)
        acc = f"{r['correct']}/{r['done']}" if r["done"] else "-"
        rate = f"{r['rate']:.1f}/h" if r["rate"] else "-"
        eta = f"{(r['target'] - r['done']) / r['rate']:.1f}h" if r["rate"] and r["done"] < r["target"] else "-"
        name = r["name"] if r["running"] else f"[dim]{r['name']}[/]"
        grid.add_row(name, bar, f"{r['done'] / r['target']:5.1%}",
                     f"{r['done']}/{r['target']}", acc, r["ago"], rate, eta)

    wd = subprocess.run(["pgrep", "-f", "rsi-watchdog.sh"], capture_output=True).returncode == 0
    foot = f"[{'green' if wd else 'red'}]{'● 看门狗在跑' if wd else '○ 看门狗没在跑'}[/] · logs/rsi-*.out · 报表 data/rsi/{{phys,math}}.md"
    return Group(tp, "", grid, "", foot)


class _Dash:
    """带 TTL 缓存的 renderable:rich 每帧重绘(转圈动画流畅),数据按间隔重采。"""

    def __init__(self, ttl: float):
        self._ttl, self._t, self._cache = ttl, 0.0, None

    def __rich_console__(self, console, options):
        now = time.time()
        if self._cache is None or now - self._t > self._ttl:
            self._cache, self._t = render_rich(), now
        yield self._cache


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("-w", "--watch", nargs="?", const=30, type=int, metavar="秒")
    p.add_argument("--plain", action="store_true", help="不用 rich,退回纯文本清屏刷新")
    args = p.parse_args()
    if args.watch is None:
        print(render())
        return
    if not args.plain:
        try:
            from rich.live import Live
        except ImportError:
            pass
        else:
            try:
                with Live(_Dash(args.watch), refresh_per_second=4, screen=False):
                    while True:
                        time.sleep(1)
            except KeyboardInterrupt:
                pass
            return
    try:
        while True:
            sys.stdout.write("\033[2J\033[H" + render() + "\n")
            sys.stdout.flush()
            time.sleep(args.watch)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

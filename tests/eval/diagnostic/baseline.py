"""Precollect closed-book answers while the server captures retrieval traces."""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from tests.eval.diagnostic.benefit import SELF_REPORT, call
from tests.eval.diagnostic.protocol import DEFAULT, dump, sha, verify
from tests.eval.hardbench.run import solve_prompt
from tests.eval.opencode_runner import INPUT_PROTOCOL, ask
from tests.eval.diagnostic.service import blocking_service_error


async def run(args):
    directory = args.directory.resolve()
    verify(directory / "inputs")
    if "-free" not in args.model:
        raise ValueError("Select an explicitly free model")
    identity = {"model": args.model, "thinking": args.thinking, "timeout": args.timeout,
                "input_delivery_protocol": INPUT_PROTOCOL,
                "runner_sha256": sha(Path(ask.__code__.co_filename).read_bytes()),
                "max_output_tokens": args.max_output_tokens,
                "input_sha256": sha((directory / "inputs/manifest.json").read_bytes()),
                "self_report_sha256": sha(SELF_REPORT), "code_sha256": sha(Path(__file__).read_bytes())}
    path = directory / "baseline-manifest.json"
    if path.exists() and json.loads(path.read_text()) != identity:
        raise ValueError("Baseline run identity changed")
    dump(path, identity)
    tasks = json.loads((directory / "inputs/tasks.json").read_text())
    # Obtain the informative paired cohort first, while retaining all planned
    # controls. Interleave domains so long competition math cannot starve physics.
    ordered = []
    for has_gold in (True, False):
        by_group = {g: [t for t in tasks if t["solve_eligible"] and bool(t["expected_ids"]) == has_gold
                         and t["group"] == g] for g in ("math", "physics")}
        while any(by_group.values()):
            for group in ("physics", "physics", "math", "physics"):
                if by_group[group]:
                    ordered.append(by_group[group].pop(0))
    jobs = [(t, a) for t in ordered
            for a in (["none1", "none2"] if t["expected_ids"] else ["none1"])]
    sem = asyncio.Semaphore(args.concurrency)
    complete = 0
    stop = asyncio.Event()
    async def one(task, arm):
        nonlocal complete
        async with sem:
            if stop.is_set():
                return
            prompt = solve_prompt(task["problem"]) + SELF_REPORT
            response = await call(directory, f"solve/{task['id']}/{arm}", prompt, args)
            complete += 1
            if blocking_service_error(response):
                stop.set()
            if complete % 10 == 0 or not response.get("ok"):
                print(f"baseline {complete}/{len(jobs)} {task['id']}/{arm} ok={response.get('ok')}", flush=True)
                dump(directory / "baseline-progress.json", {"complete": complete, "planned": len(jobs),
                                                           "stopped_for_service": stop.is_set()})
    await asyncio.gather(*(one(t, a) for t, a in jobs))
    dump(directory / "baseline-progress.json", {"complete": complete, "planned": len(jobs),
                                               "stopped_for_service": stop.is_set()})
    print(f"baseline complete {complete}/{len(jobs)}", flush=True)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--directory", type=Path, default=DEFAULT)
    p.add_argument("--model", default="opencode/deepseek-v4.1-flash-free")
    p.add_argument("--thinking", default="low")
    p.add_argument("--timeout", type=float, default=180)
    p.add_argument("--max-output-tokens", type=int, default=16384)
    p.add_argument("--concurrency", type=int, default=3)
    p.add_argument("--opencode-bin", default="/Applications/OpenCode.app/Contents/Resources/opencode-cli")
    asyncio.run(run(p.parse_args()))


if __name__ == "__main__":
    main()

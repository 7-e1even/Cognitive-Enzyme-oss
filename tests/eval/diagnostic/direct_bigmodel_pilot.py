"""One-question paired pilot through the native BigModel GLM endpoint."""
from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

from tests.eval.diagnostic.benefit import SELF_REPORT, estimates, render_memories
from tests.eval.diagnostic.protocol import append, dump, rows, sha
from tests.eval.hardbench.run import JUDGE_PROMPT, grade_local, parse_verdict, solve_prompt

ENDPOINT = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
MODEL = "glm-5.3-flash"


def request(token: str, prompt: str, *, max_tokens: int, thinking: bool, timeout: float) -> dict:
    body = {"model": MODEL, "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens, "stream": False,
            "thinking": {"type": "enabled" if thinking else "disabled"}}
    payload = json.dumps(body, ensure_ascii=False).encode()
    req = urllib.request.Request(ENDPOINT, data=payload,
                                 headers={"Authorization": "Bearer " + token,
                                          "Content-Type": "application/json"}, method="POST")
    started = time.perf_counter()
    try:
        response = urllib.request.urlopen(req, timeout=timeout)
        raw = json.loads(response.read().decode("utf-8", "replace"))
        choice = (raw.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        usage = raw.get("usage") or {}
        return {"ok": True, "text": message.get("content") or "",
                "reasoning_text": message.get("reasoning_content") or "",
                "stop": choice.get("finish_reason"), "usage": usage,
                "model": raw.get("model"), "cost": None,
                "secs": round(time.perf_counter() - started, 1),
                "tool_calls": bool(message.get("tool_calls"))}
    except urllib.error.HTTPError as exc:
        return {"ok": False, "error": exc.read().decode("utf-8", "replace")[:1200],
                "http": exc.code, "secs": round(time.perf_counter() - started, 1)}
    except Exception as exc:
        return {"ok": False, "error": type(exc).__name__ + ":" + str(exc)[:500],
                "secs": round(time.perf_counter() - started, 1)}


async def run(args):
    token = getpass.getpass()
    root = args.directory.resolve()
    root.mkdir(parents=True, exist_ok=True)
    variants = {v: root / v for v in ("legacy", "fixed")}
    tasks = json.loads((variants["legacy"] / "inputs/tasks.json").read_text())
    task = next(t for t in tasks if t["id"] == args.question_id)
    banks = json.loads((variants["legacy"] / "inputs/banks.json").read_text())
    memories = {e["id"]: e for bank in banks.values() for e in bank}
    recs = {v: {r["id"]: r for r in rows(variants[v] / "retrieval.jsonl")} for v in variants}
    output = root / "responses.jsonl"
    existing = {(r["variant"], r["arm"]) for r in rows(output)}

    async def one(variant: str, arm: str):
        if (variant, arm) in existing:
            return
        rec = recs[variant][args.question_id]
        ids = list(rec.get("injected_ids") or []) if arm == "system" else []
        block = render_memories(ids, memories, rec) if ids else ""
        prompt = (block + "\n\n" if block else "") + solve_prompt(task["problem"]) + SELF_REPORT
        response = await asyncio.to_thread(request, token, prompt, max_tokens=args.max_tokens,
                                            thinking=args.solver_thinking, timeout=args.timeout)
        row = {"question_id": args.question_id, "variant": variant, "arm": arm,
               "model": MODEL, "injected_ids": ids, "request_sha256": sha(prompt),
               "response": response, **estimates(response.get("text") or "")}
        if response.get("ok"):
            grade = grade_local(task["problem"], response.get("text") or "")
            if grade.pop("need_judge", False):
                jp = JUDGE_PROMPT.format(domain=task["problem"]["domain"],
                                         problem=task["problem"]["problem"],
                                         answer=task["problem"]["answer"], pred=grade["pred"])
                # The GLM endpoint can spend hidden reasoning tokens despite
                # thinking=disabled; 128 leaves no visible verdict.
                judge = await asyncio.to_thread(request, token, jp, max_tokens=args.judge_max_tokens,
                                                 thinking=False, timeout=args.judge_timeout)
                grade.update(correct=parse_verdict(judge.get("text") or "") if judge.get("ok") else None,
                             judge_ok=bool(judge.get("ok")),
                             grade_status="graded" if judge.get("ok") else "ungraded",
                             grader_response=judge, graded_by="judge")
            else:
                grade["grade_status"] = "graded"
            row.update(grade)
        elif response.get("error"):
            row["error"] = response["error"]
        append(output, row)

    await one("legacy", "none1")
    legacy_none = next(r for r in rows(output) if r["variant"] == "legacy" and r["arm"] == "none1")
    if ("fixed", "none1") not in existing:
        append(output, {**legacy_none, "variant": "fixed", "reused_from": "legacy/none1"})
    await one("legacy", "system")
    await one("fixed", "system")
    by = {(r["variant"], r["arm"]): r for r in rows(output)}
    status = {}
    for v in variants:
        for arm in ("none1", "system"):
            r = by.get((v, arm)); resp=(r or {}).get("response") or {}
            status[f"{v}_{arm}"]={"correct":(r or {}).get("correct"),"grade_status":(r or {}).get("grade_status","missing"),
                                  "secs":resp.get("secs"),"http":resp.get("http"),"error":(r or {}).get("error")}
    valid=all(status[k]["correct"] in (True,False) for k in ("legacy_none1","legacy_system","fixed_system"))
    summary={"protocol":"direct-bigmodel-pilot-v1","question_id":args.question_id,"model":MODEL,"status":status,
             "paired_valid":valid,"api_calls_recorded":sum(1 for _ in rows(output)),
             "note":"Only a one-question pilot; do not extrapolate to the 92-question cohort."}
    dump(root/"summary.json",summary)
    (root/"report.md").write_text("# 原生 BigModel GLM 小范围对照\n\n"+json.dumps(summary,ensure_ascii=False,indent=2)+"\n")
    print(json.dumps(summary,ensure_ascii=False,indent=2))


if __name__ == "__main__":
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--directory",type=Path,required=True)
    p.add_argument("--question-id",default="physelite-11041")
    p.add_argument("--timeout",type=float,default=120)
    p.add_argument("--judge-timeout",type=float,default=60)
    p.add_argument("--judge-max-tokens",type=int,default=1024)
    p.add_argument("--max-tokens",type=int,default=8192)
    p.add_argument("--solver-thinking", action="store_true",
                   help="Enable GLM reasoning; default is disabled for fast pilot")
    asyncio.run(run(p.parse_args()))

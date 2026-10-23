"""Blind audit of frozen question completeness and solution contamination.

The auditor never sees solver answers, correctness, retrieved memories or scores.
Flags are review candidates, not automatic authoritative ground truth.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
from pathlib import Path

from tests.eval.diagnostic.benefit import call
from tests.eval.diagnostic.protocol import DEFAULT, append, canonical, dump, rows, sha, verify
from tests.eval.opencode_runner import INPUT_PROTOCOL, ask

INSTRUCTIONS = """Audit benchmark inputs, not a model's ability. Do not solve the problems.
For every item, inspect only the supplied question and official reference.
Return a JSON array with one object per id:
{"id":"...", "completeness":"usable|incomplete|uncertain", "missing":"brief precise missing premise or empty string", "solution_in_question":false, "solution_evidence":"short exact excerpt or empty string", "reason":"brief evidence"}.
Rules:
- A mention of a figure alone does NOT make a question incomplete. All necessary geometry might be described in words.
- A symbolic answer may be valid without numeric parameter values. Mark incomplete only when a required parameter/initial setup/referent is genuinely absent and the requested/reference result cannot follow.
- Missing previous subquestions are a problem only if they contain needed givens or the requested region/event cannot be identified.
- References often contain extra subparts. Ignore reference subparts not actually requested; this alone is not an input defect.
- The reference is NOT part of the student's question: a needed number appearing only in the reference is missing from the input.
- If an Analysis/Solution paragraph or derivation is embedded in the question, set solution_in_question=true and cite an excerpt. Do not flag an ordinary given physical equation or hypothesis as a leaked solution.
- Use uncertain for doubts that require the absent diagram/source or substantial solving. Never fabricate missing information.
- Use usable only if the supplied question is sufficiently self-contained for the parts actually requested.
Only return the JSON array, no surrounding prose.
Items:
"""


def parse(text, ids):
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    value = json.loads(text)
    if not isinstance(value, list) or {r.get("id") for r in value} != set(ids) or len(value) != len(ids):
        raise ValueError("Audit id mismatch")
    if any(r.get("completeness") not in ("usable", "incomplete", "uncertain")
           or not isinstance(r.get("solution_in_question"), bool) for r in value):
        raise ValueError("Audit schema mismatch")
    return value


async def run(args):
    directory = args.directory.resolve()
    verify(directory / "inputs")
    tasks = json.loads((directory / "inputs/tasks.json").read_text())
    identity = {"model": args.model, "thinking": args.thinking, "max_tokens": args.max_output_tokens,
                "input_delivery_protocol": INPUT_PROTOCOL,
                "runner_sha256": sha(Path(ask.__code__.co_filename).read_bytes()),
                "timeout": args.timeout, "instructions_sha256": sha(INSTRUCTIONS),
                "input_sha256": sha((directory / "inputs/manifest.json").read_bytes()),
                "batch_size": args.batch_size, "blind_to_solver_and_retrieval": True,
                "interpretation": "model flags require content review; not independent human gold"}
    path = directory / "quality-manifest.json"
    if path.exists() and json.loads(path.read_text()) != identity:
        raise ValueError("Quality audit identity changed")
    dump(path, identity)
    completed = {r["id"] for r in rows(directory / "question-quality.jsonl")}
    semaphore = asyncio.Semaphore(args.concurrency)
    async def batch(items, index):
        if all(t["id"] in completed for t in items):
            return
        async with semaphore:
            public = [{"id": t["id"], "question": t["problem"]["problem"],
                       "official_reference": t["problem"]["answer"]} for t in items]
            prompt = INSTRUCTIONS + canonical(public)
            response = await call(directory, f"quality/{index}/{sha(canonical(public))[:12]}", prompt, args)
            try:
                if not response.get("ok"):
                    raise ValueError("Audit service unavailable")
                judgments = parse(response.get("text") or "", [t["id"] for t in items])
            except (ValueError, TypeError):
                judgments = [{"id": t["id"], "completeness": "uncertain", "missing": "",
                              "solution_in_question": False, "solution_evidence": "",
                              "reason": "audit unavailable/unparseable", "audit_error": True} for t in items]
            for judgment, task in ((j, next(t for t in items if t["id"] == j["id"])) for j in judgments):
                if judgment["id"] in completed:
                    continue
                append(directory / "question-quality.jsonl", {**judgment, "group": task["group"],
                       "question_sha256": sha(task["problem"]["problem"]),
                       "reference_sha256": sha(task["problem"]["answer"]), "review_status": "agent_followup_required"})
                completed.add(judgment["id"])
            print(f"question quality {len(completed)}/{len(tasks)}", flush=True)
    await asyncio.gather(*(batch(tasks[i:i+args.batch_size], i//args.batch_size)
                           for i in range(0, len(tasks), args.batch_size)))


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--directory", type=Path, default=DEFAULT)
    p.add_argument("--model", default="opencode/deepseek-v4.1-flash-free")
    p.add_argument("--thinking", default="low")
    p.add_argument("--timeout", type=float, default=120)
    p.add_argument("--max-output-tokens", type=int, default=8192)
    p.add_argument("--concurrency", type=int, default=2)
    p.add_argument("--batch-size", type=int, default=8)
    p.add_argument("--opencode-bin", default="/Applications/OpenCode.app/Contents/Resources/opencode-cli")
    asyncio.run(run(p.parse_args()))

"""Server-side transport repair; preserve the original run and reuse its APIs.

Run this file after the original collector exits. PYTHONPATH must point at the
original frozen method. Decision settings and model identities stay identical.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import time
from pathlib import Path
from types import SimpleNamespace


async def run(directory):
    from tests.eval.diagnostic import collect
    from tests.eval.diagnostic.protocol import canonical, dump, rows, sha

    destination = directory / "repair"
    destination.mkdir(exist_ok=True)
    for name in ("inputs", "embeddings.jsonl", "index-math.json", "index-physics.json"):
        target = destination / name
        if not target.exists():
            target.symlink_to(Path("..") / name)
    api = destination / "api"
    if not api.exists():
        def copy_file(src, dst):
            # Atomic JSON replacement detaches hard links. Append-only logs
            # need distinct inodes to preserve the original attempts.
            return shutil.copy2(src, dst) if str(src).endswith(".jsonl") else os.link(src, dst)
        shutil.copytree(directory / "api", api, copy_function=copy_file)
    path = destination / "retrieval.jsonl"
    source = rows(directory / "retrieval.jsonl")
    if not path.exists():
        good = [r for r in source if not r.get("error") and not r["trace"].get("rerank_failed")
                and r["trace"].get("jev_status") == "ok"]
        path.write_text("".join(canonical(r) + "\n" for r in good))
        dump(destination / "transport-repair.json", {
            "source_sha256": sha((directory / "retrieval.jsonl").read_bytes()),
            "source_questions": len(source), "reused_healthy_questions": len(good),
            "repair_questions": len(source) - len(good),
            "decision_policy": "identical; only rate pacing/retry changes",
            "rerank_spacing_seconds": 8, "retry_429_cooldown_seconds": 35,
            "script_sha256": sha(Path(__file__).read_bytes())})
    probes = destination / "gold-full-input.jsonl"
    if not probes.exists():
        good = [r for r in rows(directory / "gold-full-input.jsonl") if r.get("p") is not None]
        probes.write_text("".join(canonical(r) + "\n" for r in good))
    original = collect.RecordingHTTP
    class Paced(original):
        next_rerank = 0.0

        async def post(self, url, *, headers=None, json=None, timeout=None):
            if not url.rstrip("/").endswith("rerank"):
                return await super().post(url, headers=headers, json=json, timeout=timeout)
            key = sha(canonical({"endpoint_sha256": sha(url), "payload": json}))
            cached = self.directory / "rerank" / (key + ".json")
            if cached.exists() and __import__("json").loads(cached.read_text()).get("status") == 200:
                return await super().post(url, headers=headers, json=json, timeout=timeout)
            for _ in range(4):
                wait = max(0, self.next_rerank - time.monotonic())
                if wait:
                    await asyncio.sleep(wait)
                response = await super().post(url, headers=headers, json=json, timeout=timeout)
                self.next_rerank = time.monotonic() + (35 if response.status_code == 429 else 8)
                if response.status_code != 429:
                    return response
            return response
    collect.RecordingHTTP = Paced
    await collect.run(SimpleNamespace(directory=destination, stage="all", concurrency=1, limit=0))


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--directory", type=Path, default=Path("/evidence"))
    asyncio.run(run(p.parse_args().directory))

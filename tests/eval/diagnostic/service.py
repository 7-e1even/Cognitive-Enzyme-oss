"""Stop a collection campaign on explicit provider rejection; do not retry it."""
from __future__ import annotations

import json
import time

from tests.eval.diagnostic.protocol import dump


class ServiceBlocked(RuntimeError):
    pass


def blocking_service_error(response):
    if response.get("ok") or response.get("timed_out"):
        return False
    detail = str(response.get("error", "")).lower()
    return any(term in detail for term in (
        "rate limit", "rate-limit", "rate_limit", "too many requests", "429", "403", "402", "401",
        "free tier", "freetiererror", "quota", "unauthorized", "forbidden", "速率限制", "1302"))


def check_block(directory):
    path = directory / "service-block.json"
    if path.exists():
        record = json.loads(path.read_text())
        raise ServiceBlocked("Collection stopped after provider rejection: " + record["error"])


def record_block(directory, key, response):
    if blocking_service_error(response):
        path = directory / "service-block.json"
        if not path.exists():
            dump(path, {"key": key, "error": response["error"], "time": time.time(),
                        "policy": "no automatic retries; preserve evidence and prepare a separate recovery run"})
        raise ServiceBlocked("Collection stopped after provider rejection: " + response["error"])

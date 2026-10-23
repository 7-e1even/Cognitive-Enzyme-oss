"""Closed-book evaluation through the logged-in OpenCode v2 client.

Keep OpenCode's built-in agent and provider integration. Tool permission requests
are never approved; a tool attempt invalidates the answer. Read the durable
assistant message because v2 JSON output can omit v1's step_finish event.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import signal
import tempfile
import time

CLOSED_BOOK = ("This is a closed-book evaluation. Use only the supplied text and your own reasoning. "
               "Do not call any tools, run code, read or write files, search the web, load skills, or delegate. "
               "A tool attempt counts as a failed answer, even if permission is rejected. "
               "If you cannot solve it, give your best answer without tools. Follow the final-answer format.\n\n")

INPUT_PROTOCOL = "opencode-stdin-exact-v1"


def verify_user_input(messages: list[dict], expected: str) -> bool:
    users = [m for m in messages if m.get("type") == "user"]
    return len(users) == 1 and users[0].get("text") == expected


def parse_messages(info: dict, messages: list[dict], model: str) -> dict:
    expected = model.split("#", 1)[0]
    expected_variant = model.split("#", 1)[1] if "#" in model else None
    assistants = [m for m in messages if m.get("type") == "assistant"]
    usage = {"input": 0, "output": 0, "reasoning": 0, "cacheRead": 0}
    out = {"ok": False, "text": "", "usage": usage, "thinking_chars": 0,
           "session_id": info.get("id"), "cost": info.get("cost")}
    if not assistants:
        return {**out, "error": "no durable assistant message"}
    for message in assistants:
        actual = message.get("model") or {}
        served = f"{actual.get('providerID')}/{actual.get('id')}"
        if served != expected:
            return {**out, "error": f"unexpected model: {served}"}
        if expected_variant and actual.get("variant") != expected_variant:
            return {**out, "error": f"unexpected model variant: {actual.get('variant')}"}
        if message.get("error"):
            return {**out, "error": str(message["error"].get("message") or message["error"])[:700]}
        tokens = message.get("tokens") or {}
        for key in ("input", "output", "reasoning"):
            usage[key] += tokens.get(key) or 0
        usage["cacheRead"] += (tokens.get("cache") or {}).get("read") or 0
        parts = message.get("content") or []
        if any(p.get("type") == "tool" for p in parts):
            return {**out, "error": "tool attempt invalidates closed-book evaluation",
                    "protocol_error": "tool_attempt",
                    "tool_attempts": [p.get("name") for p in parts if p.get("type") == "tool"]}
        out["thinking_chars"] += sum(len(p.get("text", "")) for p in parts if p.get("type") == "reasoning")
    if any(m.get("type") in ("skill", "shell") for m in messages):
        return {**out, "error": "external context invalidates closed-book evaluation"}
    if expected.endswith("-free") and (info.get("cost") or 0) > 0:
        return {**out, "error": "nonzero cost on a free-model run"}
    last = assistants[-1]
    out.update(text="".join(p.get("text", "") for p in last.get("content", []) if p.get("type") == "text"),
               stop=last.get("finish"), served_model=expected + "#" + (last.get("model", {}).get("variant") or "default"))
    out["ok"] = info.get("outcome") == "succeeded" and (
        out["stop"] == "length" or (out["stop"] == "stop" and bool(out["text"].strip())))
    if not out["ok"]:
        out["error"] = f"incomplete OpenCode answer: outcome={info.get('outcome')}, finish={out['stop']}"
    return out


async def ask(prompt: str, args, env: dict, executable: str) -> dict:
    if getattr(args, "model", "").split("#", 1)[0].startswith("zcode/"):
        from tests.eval.zcode_runner import ask as ask_zcode
        return await ask_zcode(prompt, args, env, executable)
    t0 = time.perf_counter()
    variant = "low" if getattr(args, "thinking", "low") == "off" else getattr(args, "thinking", "low")
    model = args.model if "#" in args.model else f"{args.model}#{variant}"
    permissions = [{"action": "*", "resource": "*", "effect": "ask"}]
    config = {"model": model, "share": "disabled", "snapshots": False,
              "permissions": permissions, "agents": {"build": {
                  "permissions": permissions,
                  "request": {"body": {"max_tokens": getattr(args, "max_output_tokens", 16384)}}}}}
    child_env = dict(env)
    # A private server reads the desktop's credentials without copying them.
    # Do not disable the built-in provider plugins or replace its system prompt.
    child_env.pop("OPENCODE_PERMISSION", None)
    child_env.pop("OPENCODE_DISABLE_DEFAULT_PLUGINS", None)
    child_env["OPENCODE_CONFIG_CONTENT"] = json.dumps(config)

    async def api(operation: str, sid: str) -> dict:
        command = [executable, "api", operation, "--param", f"sessionID={sid}"]
        if operation == "session.message.list":
            command += ["--param", "order=asc"]
        proc = await asyncio.create_subprocess_exec(*command, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env)
        try:
            data, error = await asyncio.wait_for(proc.communicate(), 20)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            proc.kill()
            await proc.wait()
            raise
        if proc.returncode:
            raise ValueError(f"OpenCode {operation} failed: {error.decode(errors='replace')[-300:]}")
        return json.loads(data)

    with tempfile.TemporaryDirectory(prefix="ce-opencode-eval-") as directory:
        child_env["PWD"] = directory
        # v2's positional argument parser can quote/repeat a message. Stdin is
        # the supported path that preserves the exact input; verify durable text.
        expected_input = CLOSED_BOOK + prompt
        command = [executable, "run", "--standalone", "--format", "json", "--thinking",
                   "--agent", "build", "--model", model]
        proc = await asyncio.create_subprocess_exec(*command, cwd=directory, env=child_env,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, start_new_session=True)
        try:
            raw, stderr = await asyncio.wait_for(proc.communicate(expected_input.encode()), args.timeout)
        except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await proc.wait()
            if isinstance(exc, asyncio.CancelledError):
                raise
            return {"ok": False, "error": f"timeout {args.timeout:.0f}s", "timed_out": True,
                    "secs": round(time.perf_counter() - t0, 1)}
        events = []
        for line in raw.decode(errors="replace").splitlines():
            try:
                events.append(json.loads(line))
            except ValueError:
                continue
        errors = [e.get("error") or {} for e in events if e.get("type") == "error"]
        if errors or proc.returncode:
            detail = "; ".join(str(e.get("message") or e) for e in errors) or stderr.decode(errors="replace")[-500:]
            return {"ok": False, "error": detail[:1000], "secs": round(time.perf_counter() - t0, 1)}
        sid = next((e["sessionID"] for e in events if e.get("sessionID")), None)
        if not sid:
            return {"ok": False, "error": "no OpenCode session identity", "secs": round(time.perf_counter() - t0, 1)}
        try:
            info = (await api("session.get", sid))["data"]
            messages = (await api("session.message.list", sid))["data"]
            out = parse_messages(info, messages, model)
            verified = verify_user_input(messages, expected_input)
            out.update(input_delivery_protocol=INPUT_PROTOCOL, input_delivery_verified=verified,
                       delivered_input_sha256=hashlib.sha256(expected_input.encode()).hexdigest() if verified else None)
            if not verified:
                out.update(ok=False, error="durable user input differs from the intended closed-book prompt",
                           harness_error="input_delivery_mismatch")
        except (ValueError, KeyError, asyncio.TimeoutError) as exc:
            out = {"ok": False, "error": f"OpenCode result verification failed: {exc}", "session_id": sid}
        out["secs"] = round(time.perf_counter() - t0, 1)
        return out

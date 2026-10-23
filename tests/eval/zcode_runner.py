"""Run the diagnostic solver through the logged-in ZCode GLM provider.

ZCode's bundled CLI does not expose a model flag.  The runner creates a
temporary provider configuration that orders the already-authenticated
``magpie`` provider and ``zcode/GLM-5.3-Flash`` first.  The user's ZCode files
are never modified and credentials are read from the existing encrypted store.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import shutil
import signal
import tempfile
import time
from pathlib import Path

from tests.eval.opencode_runner import CLOSED_BOOK

MODEL = "zcode/GLM-5.3-Flash"
ALLOWED_MODELS = {
    "zcode/GLM-5.3-Flash": {"low", "high", "max"},
    "zcode/GLM-5.3": {"low", "high", "max"},
    "zcode/GLM-5.2": {"disabled", "high", "max"},
    "zcode/GLM-5-Turbo": {"disabled", "high"},
}
INPUT_PROTOCOL = "zcode-prompt-exact-v1"
ZCODE_CLI = Path("/Applications/ZCode.app/Contents/Resources/glm/zcode.cjs")
ZCODE_BUILTIN = Path("/Applications/ZCode.app/Contents/Resources/config/provider/zcode-builtin.json")
PERSONAL_CONFIG = Path.home() / ".zcode/v2/provider_config.json"

# The prompt explicitly forbids tools; this list also removes the built-in
# agent tools so an accidental tool request is observable as a failed trial.
DISALLOWED_TOOLS = (
    "Agent,AskUserQuestion,Bash,Edit,EnterPlanMode,ExitPlanMode,Read,Skill,"
    "TaskOutput,TaskStop,TodoRead,TodoWrite,WebFetch,WebSearch,Write,SendMessage,"
    "ReadSessionContext,ReplyToChannel,NotebookEdit,Glob,Grep,Task"
)


def _provider_config(path: Path, model: str, output: Path) -> Path:
    if not path.exists():
        raise ValueError("ZCode personal provider config is missing")
    data = json.loads(path.read_text())
    config = data.get("config") or {}
    rules = config.get("providerConfigRules", {}).get("providerRules", [])
    magpie = next((r for r in rules if r.get("providerId") == "magpie"), None)
    if magpie is None:
        raise ValueError("ZCode magpie provider is not configured")
    data = copy.deepcopy(data)
    config = data["config"]
    rules = config["providerConfigRules"]["providerRules"]
    magpie = next(r for r in rules if r.get("providerId") == "magpie")
    base = model.split("$", 1)[0]
    models = [base, "zcode/GLM-5.3", "zcode/GLM-5.2", "zcode/GLM-5-Turbo"]
    magpie["config"]["personalModelIds"] = models
    magpie["config"]["modelOrder"] = models
    config["providerConfigRules"]["providerRules"] = [magpie]
    config["providerOrder"] = ["magpie"]
    if "$" in model:
        config["defaultModelSelection"] = {
            "providerId": "magpie", "modelId": base,
            "options": {"reasoningLevel": model.split("$", 1)[1]},
        }
    config["modelConfigRules"]["providerModelRules"] = [
        rule for rule in config.get("modelConfigRules", {}).get("providerModelRules", [])
        if rule.get("providerId") == "magpie" and rule.get("modelId", "").startswith("zcode/GLM-")
    ]
    out = output
    out.write_text(json.dumps(data, ensure_ascii=False))
    return out


def _rollout(session_id: str) -> dict | None:
    path = Path.home() / ".zcode/cli/rollout" / f"model-io-{session_id}.jsonl"
    if not path.exists():
        return None
    lines = [line for line in path.read_text(errors="replace").splitlines() if line.strip()]
    if not lines:
        return None
    return json.loads(lines[-1])


def _verified_request(record: dict | None, expected: str, model: str) -> tuple[bool, dict]:
    if not record:
        return False, {"reason": "missing ZCode model-io audit record"}
    actual = record.get("model") or {}
    served = f"{actual.get('providerId')}/{actual.get('modelId')}"
    messages = (record.get("request") or {}).get("messages") or []
    exact = [m for m in messages if m.get("role") == "user" and m.get("content") == expected]
    response = record.get("response") or {}
    tools = response.get("toolCalls") or []
    # ``model`` is the ZCode picker id (zcode/GLM-5.3-Flash); the rollout
    # records the proxy provider separately as magpie/zcode/GLM-5.3-Flash.
    verified = actual.get("modelId") == model and len(exact) == 1 and not tools
    return verified, {"served_model": served, "exact_user_messages": len(exact),
                      "tool_calls": len(tools), "rollout": str(record.get("sessionId") or "")}


async def ask(prompt: str, args, env: dict, executable: str | None = None) -> dict:
    started = time.perf_counter()
    model = getattr(args, "model", MODEL).split("#", 1)[0]
    base_model = model.split("$", 1)[0]
    if base_model not in ALLOWED_MODELS:
        raise ValueError(f"ZCode runner only permits configured GLM models: {sorted(ALLOWED_MODELS)}")
    if "$" in model and model.split("$", 1)[1] not in ALLOWED_MODELS[base_model]:
        raise ValueError(f"Unsupported reasoning level for {base_model}")
    if not ZCODE_CLI.exists() or not ZCODE_BUILTIN.exists():
        return {"ok": False, "error": "ZCode bundled CLI/provider config is missing",
                "input_delivery_protocol": INPUT_PROTOCOL, "secs": round(time.perf_counter() - started, 1)}
    expected = CLOSED_BOOK + prompt
    node = shutil.which("node")
    if not node:
        return {"ok": False, "error": "node executable is missing", "input_delivery_protocol": INPUT_PROTOCOL}
    with tempfile.TemporaryDirectory(prefix="ce-zcode-eval-") as temp:
        root = Path(temp)
        # zcode.cjs's non-SEA fallback expects this relative bundle layout.
        entry = root / "a/b/c/d/e/zcode.cjs"
        entry.parent.mkdir(parents=True)
        shutil.copy2(ZCODE_CLI, entry)
        work = root / "work"
        work.mkdir()
        personal = _provider_config(PERSONAL_CONFIG, model, root / "provider_config.json")
        child_env = dict(env)
        child_env.update({
            "ZCODE_BUILTIN_PROVIDER_CONFIG_FILE": str(ZCODE_BUILTIN),
            "ZCODE_PERSONAL_PROVIDER_CONFIG_FILE": str(personal),
        })
        command = [node, str(entry), "--prompt", expected, "--json", "--no-browser",
                   "--mode", getattr(args, "zcode_mode", "build"), "--workflow-mode", "disabled",
                   "--disallowed-tools", DISALLOWED_TOOLS, "--cwd", str(work)]
        proc = await asyncio.create_subprocess_exec(
            *command, cwd=str(work), env=child_env,
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, start_new_session=True)
        try:
            raw, stderr = await asyncio.wait_for(proc.communicate(), getattr(args, "timeout", 180))
        except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await proc.wait()
            if isinstance(exc, asyncio.CancelledError):
                raise
            return {"ok": False, "error": f"timeout {getattr(args, 'timeout', 180):.0f}s",
                    "timed_out": True, "input_delivery_protocol": INPUT_PROTOCOL,
                    "secs": round(time.perf_counter() - started, 1)}
        text = raw.decode(errors="replace").strip()
        try:
            result = json.loads(text)
        except ValueError:
            detail = stderr.decode(errors="replace")[-1000:] or text[-1000:]
            return {"ok": False, "error": detail or "invalid ZCode JSON output",
                    "input_delivery_protocol": INPUT_PROTOCOL, "secs": round(time.perf_counter() - started, 1)}
        session_id = result.get("sessionId")
        audit = _rollout(session_id) if session_id else None
        verified, audit_meta = _verified_request(audit, expected, base_model)
        response_text = result.get("response") or ""
        audit_response = (audit or {}).get("response") or {}
        finish = audit_response.get("finishReason") or "stop"
        usage = result.get("usage") or audit_response.get("usage") or {}
        out = {
            "ok": proc.returncode == 0 and bool(response_text.strip()) and verified,
            "text": response_text,
            "usage": {"input": usage.get("inputTokens", 0),
                       "output": usage.get("outputTokens", 0),
                       "reasoning": usage.get("reasoningTokens", 0),
                       "cacheRead": usage.get("cacheReadTokens", 0)},
            "thinking_chars": len(audit_response.get("reasoningText") or ""),
            "session_id": session_id,
            "cost": None,
            "stop": finish,
            "served_model": audit_meta.get("served_model"),
            "input_delivery_protocol": INPUT_PROTOCOL,
            "input_delivery_verified": verified,
            "delivered_input_sha256": hashlib.sha256(expected.encode()).hexdigest() if verified else None,
            "zcode_audit": audit_meta,
            "secs": round(time.perf_counter() - started, 1),
        }
        if audit_meta.get("tool_calls"):
            out.update(ok=False, error="tool attempt invalidates closed-book evaluation",
                       protocol_error="tool_attempt")
        elif not verified:
            out.update(ok=False, error="ZCode model or exact prompt audit failed",
                       harness_error="input_delivery_mismatch")
        elif proc.returncode:
            out.update(ok=False, error=stderr.decode(errors="replace")[-1000:] or "ZCode failed")
        elif not response_text.strip():
            out.update(ok=False, error="ZCode returned an empty answer")
        return out


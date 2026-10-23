"""Privacy scrubber — 在记忆写路径入口剥离凭证/密钥类敏感信息。

会话 transcript 与 harvest 洞察都可能夹带 API key、token、密码。
这些一旦被提炼进酶库,会随 prime 注入扩散到其他会话。
在写入前做确定性正则脱敏,统一替换为 [REDACTED] 占位。
"""

from __future__ import annotations

import re

REDACTED = "[REDACTED]"

# (pattern, replacement) 顺序执行;replacement 可用反向引用保留上下文。
_RULES: list[tuple[re.Pattern[str], str]] = [
    # PEM 私钥块
    (
        re.compile(
            r"-----BEGIN[A-Z ]*PRIVATE KEY-----.*?-----END[A-Z ]*PRIVATE KEY-----",
            re.DOTALL,
        ),
        REDACTED,
    ),
    # OpenAI / Anthropic 风格 key(sk-... / sk-ant-...)
    (re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_-]{20,}\b"), REDACTED),
    # GitHub token
    (re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b"), REDACTED),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"), REDACTED),
    # AWS access key id
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), REDACTED),
    # Slack / Google API key
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"), REDACTED),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"), REDACTED),
    # JWT(三段 base64url)
    (
        re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}\b"),
        REDACTED,
    ),
    # Authorization: Bearer <token>
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{16,}"), f"Bearer {REDACTED}"),
    # key=value / key: value 形式的凭证赋值(保留键名)
    (
        re.compile(
            r"(?i)\b((?:api[_-]?key|api[_-]?token|access[_-]?token|auth[_-]?token"
            r"|secret[_-]?key|client[_-]?secret|password|passwd)\s*[:=]\s*)"
            r"(['\"]?)[^\s'\"]{6,}\2"
        ),
        r"\g<1>\g<2>" + REDACTED + r"\g<2>",
    ),
    # URL 内嵌凭证 scheme://user:pass@host
    (re.compile(r"(://[^/\s:@]+:)[^@/\s]+(@)"), r"\g<1>" + REDACTED + r"\g<2>"),
]


def scrub_secrets(text: str) -> str:
    """返回脱敏后的文本;空值原样返回。"""
    if not text:
        return text
    for pattern, replacement in _RULES:
        text = pattern.sub(replacement, text)
    return text

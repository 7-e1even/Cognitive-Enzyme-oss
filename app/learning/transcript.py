"""对话记录的长度上限——提炼经验、被动评估、情景卡共用。

长会话里最有用的往往在结尾(最后是怎么解决的),开头交代了任务是什么;超出上限时保留开头一小段和
大部分结尾,中间标明省略了多少。上限是运行参数 learning.capture_chars:长上下文模型(如 GLM 1M)
可以放得很宽,上下文较短的模型要调小,否则请求会超出模型的上下文。
"""

from __future__ import annotations

from app import settings

_HEAD_SHARE = 0.1


def clip_transcript(text: str, limit: int | None = None) -> str:
    limit = limit or settings.get("learning.capture_chars")
    if len(text) <= limit:
        return text
    head = int(limit * _HEAD_SHARE)
    return f"{text[:head]}\n\n…(中间省略 {len(text) - limit} 字)…\n\n{text[len(text) - (limit - head):]}"

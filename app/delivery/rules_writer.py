"""Rules writer — generate and write user_rules file from mature enzymes."""

from __future__ import annotations

import logging
from pathlib import Path

from app.core import Enzyme

logger = logging.getLogger(__name__)


def generate_rules_content(enzymes: list[Enzyme]) -> str:
    """Generate rules text from mature enzymes."""
    if not enzymes:
        return "# 认知酶·安全直觉\n\n暂无成熟规则。\n"

    lines = ["# 认知酶·安全直觉\n"]
    lines.append("以下是经过实战验证的安全直觉，请在工作中自动应用：\n")

    for e in enzymes:
        line = f"- 看到 {e.patterns_readable} → {e.then}"
        if e.because:
            line += f"（{e.because}）"
        lines.append(line)

    lines.append("")  # trailing newline
    return "\n".join(lines)


def write_rules(path: str, content: str) -> None:
    """Write rules content to file."""
    if not path:
        logger.debug("No rules_path configured, skipping write")
        return

    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    logger.info("Rules written to %s", path)

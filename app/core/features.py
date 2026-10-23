"""Feature extraction — scan context to extract searchable features."""

from __future__ import annotations

import os
import re
from pathlib import Path


# 技术栈识别规则
_TECH_MARKERS: dict[str, list[str]] = {
    "java": ["pom.xml", "build.gradle", ".java", "mvnw"],
    "spring": ["application.properties", "application.yml", "SpringApplication"],
    "python": ["requirements.txt", "pyproject.toml", "setup.py", ".py"],
    "node": ["package.json", "node_modules", ".js", ".ts"],
    "php": ["composer.json", ".php"],
    "go": ["go.mod", "go.sum", ".go"],
    "docker": ["Dockerfile", "docker-compose.yml", "docker-compose.yaml"],
}

# 安全相关关键词（扫描代码内容时使用）
_SECURITY_KEYWORDS: list[str] = [
    "readObject", "ObjectInputStream", "deserialize", "unmarshal",
    "Runtime.exec", "ProcessBuilder", "eval(", "exec(",
    "JNDI", "RMI", "LDAP", "JMX",
    "Fastjson", "Jackson", "Gson", "XStream", "SnakeYAML",
    "TemplatesImpl", "InvokerTransformer", "ChainedTransformer",
    "SpringBoot", "Actuator", "Shiro", "Struts",
    "upload", "FileUpload", "MultipartFile",
    "SQL", "PreparedStatement", "createQuery",
    "SSTI", "Thymeleaf", "Freemarker", "Velocity",
    "XSS", "innerHTML", "document.write",
    "SSRF", "URLConnection", "HttpClient",
    "JWT", "token", "Bearer",
    "password", "secret", "credential", "api_key",
]


PROJECT_TAG_PREFIX = "project:"  # 酶的项目出身标签,召回时同项目加权/异项目降权


def project_slug(cwd: str) -> str:
    """从工作区路径推项目标识:目录名小写、只留 [a-z0-9._-]。

    空/根路径返回 ""(视为无项目维度)。容器内 cwd 恒为 /workspace,
    没有信息量 —— 调用方应优先用显式 project(hook 的 CE_PROJECT env)。
    """
    if not cwd:
        return ""
    name = os.path.basename(os.path.normpath(cwd.replace("\\", "/"))).lower()
    slug = re.sub(r"[^a-z0-9._-]+", "-", name).strip("-.")
    return "" if slug in ("", "workspace") else slug


def scan_workspace(workspace: str) -> dict:
    """Scan workspace directory to extract features.

    Returns dict with:
        - techs: detected technology stacks
        - keywords: security-relevant keywords found
        - files: notable file names
    """
    workspace_path = Path(workspace)
    if not workspace_path.exists():
        return {"techs": [], "keywords": [], "files": []}

    techs: set[str] = set()
    keywords: set[str] = set()
    notable_files: list[str] = []

    # Walk dir (shallow — max 3 levels deep, skip heavy dirs)
    skip_dirs = {".git", "node_modules", "__pycache__", ".venv", "venv", "target", "build", "dist"}

    for root, dirs, files in os.walk(workspace, topdown=True):
        # Depth control
        depth = root.replace(workspace, "").count(os.sep)
        if depth >= 3:
            dirs.clear()
            continue

        # Skip heavy dirs
        dirs[:] = [d for d in dirs if d not in skip_dirs]

        for fname in files:
            # Tech detection
            for tech, markers in _TECH_MARKERS.items():
                if any(fname.endswith(m) or fname == m for m in markers):
                    techs.add(tech)

            # Notable files
            if fname in ("pom.xml", "build.gradle", "package.json", "requirements.txt",
                         "Dockerfile", "docker-compose.yml"):
                notable_files.append(fname)

    return {
        "techs": sorted(techs),
        "keywords": [],  # keywords 在 prime() 时从具体内容提取
        "files": notable_files,
    }


def extract_keywords(text: str) -> list[str]:
    """Extract security-relevant keywords from text content."""
    found = []
    text_lower = text.lower()
    for kw in _SECURITY_KEYWORDS:
        if kw.lower() in text_lower:
            found.append(kw)
    return found


def build_search_query(context: str, features: dict | None = None) -> str:
    """Build a search query from context + features for Meilisearch."""
    parts = [context]

    if features:
        if features.get("techs"):
            parts.extend(features["techs"])
        if features.get("keywords"):
            parts.extend(features["keywords"][:10])

    # Extract keywords from context
    ctx_keywords = extract_keywords(context)
    parts.extend(ctx_keywords[:5])

    return " ".join(parts[:20])  # Meili handles long queries well

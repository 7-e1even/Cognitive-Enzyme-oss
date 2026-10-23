"""隐私脱敏单测 — 纯函数,无需起服务/连库。

直接跑: python tests/test_privacy.py
覆盖:各类凭证格式的剥离、上下文保留、无敏感内容时的原样返回。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.learning.privacy import REDACTED, scrub_secrets  # noqa: E402


def test_openai_style_keys():
    out = scrub_secrets("配置 sk-proj-abcDEF1234567890abcDEF12 到环境里")
    assert "sk-proj" not in out and REDACTED in out
    out = scrub_secrets("anthropic key: sk-ant-api03-abcdefghijklmnopqrstuv")
    assert "sk-ant" not in out


def test_github_and_aws():
    out = scrub_secrets("用 ghp_abcdefghijklmnopqrstuvwxyz123456 拉私有仓库")
    assert "ghp_" not in out
    out = scrub_secrets("AKIAIOSFODNN7EXAMPLE 是 access key id")
    assert "AKIA" not in out


def test_jwt_and_bearer():
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9P"
    out = scrub_secrets(f"header 里带 {jwt}")
    assert jwt not in out
    out = scrub_secrets("Authorization: Bearer abcdefghijklmnop1234567890")
    assert "abcdefghijklmnop" not in out
    assert "Bearer" in out  # 保留上下文语义


def test_key_value_assignments():
    out = scrub_secrets("记得设置 API_KEY=super-secret-value-123")
    assert "super-secret-value-123" not in out
    assert "API_KEY" in out  # 键名保留
    out = scrub_secrets('password: "hunter2hunter2"')
    assert "hunter2" not in out


def test_url_credentials():
    out = scrub_secrets("连接串 postgresql://enzyme:s3cretpw@localhost:5432/db")
    assert "s3cretpw" not in out
    assert "postgresql://enzyme:" in out and "@localhost" in out


def test_pem_block():
    pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIEow...\n-----END RSA PRIVATE KEY-----"
    out = scrub_secrets(f"密钥文件内容:\n{pem}\n保存到 ~/.ssh")
    assert "BEGIN RSA" not in out
    assert "保存到 ~/.ssh" in out


def test_clean_text_untouched():
    text = "在 FastAPI 里用 Depends 注入 engine;risk-management 模块无关密钥"
    assert scrub_secrets(text) == text
    assert scrub_secrets("") == ""


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"[ok] {name}")
            except AssertionError as e:
                failed += 1
                print(f"[fail] {name}: {e}")
    if failed:
        sys.exit(f"{failed} test(s) failed")
    print("all privacy tests passed")

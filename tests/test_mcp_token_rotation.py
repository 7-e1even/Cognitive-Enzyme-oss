"""MCP 令牌运行中更换:已启用认证时即时生效,清空立即作废,从无到有需重启。"""

import pytest

from app import mcp_server


@pytest.fixture(autouse=True)
def _restore_auth():
    saved = mcp_server.mcp.auth
    yield
    mcp_server.mcp.auth = saved


@pytest.mark.asyncio
async def test_rotate_live_when_auth_active():
    mcp_server.apply_mcp_auth("old-token-0123456789")
    assert mcp_server.mcp_auth_active()
    assert mcp_server.update_mcp_token("new-token-0123456789") is True
    verifier = mcp_server.mcp.auth
    assert await verifier.verify_token("new-token-0123456789") is not None
    assert await verifier.verify_token("old-token-0123456789") is None


@pytest.mark.asyncio
async def test_clear_revokes_immediately_but_needs_restart():
    mcp_server.apply_mcp_auth("old-token-0123456789")
    assert mcp_server.update_mcp_token("") is False
    assert await mcp_server.mcp.auth.verify_token("old-token-0123456789") is None


def test_enabling_from_open_needs_restart():
    mcp_server.apply_mcp_auth("")
    assert not mcp_server.mcp_auth_active()
    assert mcp_server.update_mcp_token("fresh-token-0123456789") is False
    assert mcp_server.update_mcp_token("") is True

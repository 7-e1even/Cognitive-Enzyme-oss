"""Hosted MCP must reuse the REST Engine lifecycle."""

import pytest

from app import mcp_server


class FakeEngine:
    def __init__(self):
        self.pg = object()
        self.qdrant = object()
        self.config = object()


@pytest.mark.asyncio
async def test_hosted_mcp_reuses_shared_engine():
    engine = FakeEngine()
    mcp_server.set_shared_engine(engine)
    try:
        async with mcp_server.lifespan(None) as context:
            assert context["engine"] is engine
            assert context["pg"] is engine.pg
            assert context["qdrant"] is engine.qdrant
    finally:
        mcp_server.set_shared_engine(None)

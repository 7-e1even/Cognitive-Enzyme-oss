"""Static machine-token scope regression tests."""

import pytest
from starlette.requests import Request
from starlette.responses import Response

from app.security import (
    configure,
    configure_api_token,
    machine_api_access_allowed,
)


def test_hook_and_gardener_data_plane_routes_are_allowed():
    allowed = [
        ("POST", "/api/prime"),
        ("POST", "/api/capture"),
        ("POST", "/api/report"),
        ("POST", "/api/harvest"),
        ("POST", "/api/recall"),
        ("GET", "/api/enzymes"),
        ("GET", "/api/episode/episode-1"),
        ("GET", "/api/metrics"),
    ]
    assert all(machine_api_access_allowed(method, path) for method, path in allowed)


def test_control_plane_routes_are_denied():
    denied = [
        ("PUT", "/api/gardener/config"),
        ("POST", "/api/gardener/run"),
        ("PUT", "/api/consolidation/schedule"),
        ("POST", "/api/consolidation/run"),
        ("PUT", "/api/admin/security/tokens/api"),
        ("PATCH", "/api/admin/settings"),
        ("POST", "/api/admin/providers"),
        ("PUT", "/api/admin/models/roles"),
        ("POST", "/api/admin/users"),
        ("POST", "/api/enzymes/bulk"),
        ("DELETE", "/api/enzymes/enzyme-1"),
        ("POST", "/api/enzymes"),
    ]
    assert not any(machine_api_access_allowed(method, path) for method, path in denied)


def _request(method: str, path: str, token: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": method,
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": [(b"authorization", f"Bearer {token}".encode())],
            "scheme": "http",
            "server": ("test", 80),
            "client": ("test", 1234),
            "root_path": "",
            "http_version": "1.1",
        }
    )


@pytest.mark.asyncio
async def test_auth_gate_enforces_machine_scope():
    from app.main import auth_gate
    import app.security as security

    original_state = dict(security._STATE)
    configure(secret="test-secret", enabled=True)
    configure_api_token("machine-token")

    async def call_next(_request):
        return Response(status_code=204)

    try:
        allowed = await auth_gate(
            _request("POST", "/api/capture", "machine-token"), call_next
        )
        denied = await auth_gate(
            _request("POST", "/api/gardener/run", "machine-token"), call_next
        )
        assert allowed.status_code == 204
        assert denied.status_code == 403
    finally:
        security._STATE.clear()
        security._STATE.update(original_state)

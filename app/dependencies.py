"""FastAPI dependencies — shared state via app.state."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import Request

if TYPE_CHECKING:
    from app.config import Config
    from app.core.engine import Engine


def get_engine(request: Request) -> "Engine":
    return request.app.state.engine


def get_config(request: Request) -> "Config":
    return request.app.state.config

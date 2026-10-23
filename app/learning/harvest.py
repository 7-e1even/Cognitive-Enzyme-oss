"""Harvest — AI proactively contributes learned experience.

Core harvest logic is in engine.py. This module re-exports the pattern
extraction from genesis for convenience.
"""

from app.learning.genesis import extract_patterns

__all__ = ["extract_patterns"]

"""External RenderDoc AI companion.

This package is deliberately hosted outside qrenderdoc.  It owns the model
connection, consumes the local MCP server over stdio, and treats every replay
mutation as a separately authorised operation.
"""

from .engine import CompanionConfig, CompanionEngine, CompanionError
from .policy import parse_authorized_message

__all__ = [
    "CompanionConfig",
    "CompanionEngine",
    "CompanionError",
    "parse_authorized_message",
]

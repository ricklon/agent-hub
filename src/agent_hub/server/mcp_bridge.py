"""MCP tool bridge between agents (Phase 2 stub).

Will route MCP tool calls from one agent to another agent's exposed
tool server. Not implemented yet.
"""

from fastapi import APIRouter


def make_router() -> APIRouter:
    """Return an empty router placeholder for the MCP bridge.

    Returns:
        APIRouter with no routes — populated in Phase 2.
    """
    return APIRouter()

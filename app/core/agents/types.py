"""Thin type module for agent-runner primitives.

Separated from ``app.core.agents.base`` so that ``app.integrations.base``
can import ``RegisteredTool`` without pulling in the full base module and
creating a circular import chain:

    agents.base  →  integrations.base  →  agents.base   ← cycle
    agents.base  →  integrations.base  →  agents.types  ← safe

All three primitives here are re-exported from ``agents.base`` for backwards
compatibility — existing code that does ``from app.core.agents.base import
RegisteredTool`` continues to work.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from app.core.llm.client import ToolSpec

# A tool handler receives the model's parsed arguments and returns a string for the model.
# It is given `dry_run` so write actions can describe themselves instead of happening.
ToolHandler = Callable[[dict[str, Any], bool], Awaitable[str]]


@dataclass
class RegisteredTool:
    """A tool spec paired with its async handler.

    ``spec`` is the :class:`~app.core.llm.client.ToolSpec` that describes the
    tool to the LLM; ``handler`` is called with the model's parsed arguments.
    """

    spec: ToolSpec
    handler: ToolHandler


class AgentRunError(RuntimeError):
    """Run could not start. Failures *during* a run are recorded on the session instead."""

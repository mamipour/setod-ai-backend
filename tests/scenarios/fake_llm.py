"""Fake LLM client for scenario testing.

The fake drives the agent loop by scripting which tool calls to make in order,
then emitting a final end-turn response.  This lets tests assert on tool
sequences without touching a real LLM or paying API costs.

Usage::

    client = FakeLLMClient(tool_sequence=["send_email", "append_row"])
    # pass client as the `tools` kwarg to run_agent
"""

from __future__ import annotations

import uuid
from typing import Any

from app.core.llm.client import LLMResponse, ToolCall, ToolSpec


class FakeLLMClient:
    """Scripted client that emits one tool call per turn until the list is exhausted.

    On the last turn it returns an `end_turn` text response so the loop terminates
    cleanly.  Arguments are minimal but valid for the tool's JSON Schema.

    Attributes
    ----------
    calls_made:
        Ordered list of tool names actually requested by the fake, populated as
        :meth:`chat` is called.  Assertions compare this against `expected_tools`.
    """

    def __init__(self, tool_sequence: list[str]) -> None:
        self._queue: list[str] = list(tool_sequence)
        self.calls_made: list[str] = []

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[ToolSpec] | None = None,
        max_tokens: int = 4096,
        temperature: float | None = None,
    ) -> LLMResponse:
        if not self._queue:
            return LLMResponse(
                content="Done.",
                tool_calls=[],
                stop_reason="end_turn",
            )

        next_tool = self._queue.pop(0)
        self.calls_made.append(next_tool)

        # Find the spec so we can fabricate minimally-valid arguments.
        spec = _find_spec(next_tool, tools or [])
        args = _minimal_args(spec) if spec else {}

        return LLMResponse(
            content="",
            tool_calls=[ToolCall(id=str(uuid.uuid4()), name=next_tool, arguments=args)],
            stop_reason="tool_use",
        )


# ── helpers ───────────────────────────────────────────────────────────────────

def _find_spec(name: str, specs: list[ToolSpec]) -> ToolSpec | None:
    for s in specs:
        if s.name == name:
            return s
    return None


def _minimal_args(spec: ToolSpec) -> dict[str, Any]:
    """Build the smallest valid argument dict from a JSON Schema."""
    props: dict[str, Any] = spec.parameters.get("properties", {})
    required: list[str] = spec.parameters.get("required", [])
    args: dict[str, Any] = {}
    for key in required:
        schema = props.get(key, {})
        typ = schema.get("type", "string")
        if typ == "string":
            args[key] = f"test_{key}"
        elif typ == "integer":
            args[key] = 1
        elif typ == "number":
            args[key] = 1.0
        elif typ == "boolean":
            args[key] = False
        elif typ == "array":
            args[key] = []
        elif typ == "object":
            args[key] = {}
        else:
            args[key] = None
    return args

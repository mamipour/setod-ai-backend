"""
Provider shim
=============
One interface over OpenAI and Anthropic so the agent loop never has to care which one an
org picked.

The internal message and tool formats follow OpenAI's shape because it is the flatter of the
two, and Anthropic is adapted to it here:

  * OpenAI carries the system prompt as the first message; Anthropic takes it as a
    top-level parameter.
  * OpenAI represents a tool call as `assistant.tool_calls[]` and its result as a separate
    `role="tool"` message; Anthropic puts both inside content blocks — `tool_use` on an
    assistant message, `tool_result` on a *user* message.
  * OpenAI names the JSON Schema field `parameters`; Anthropic names it `input_schema`.
  * OpenAI returns arguments as a JSON string; Anthropic returns a parsed dict.

Keeping all of that here means the loop deals in one vocabulary.
"""

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Literal

import anthropic
import openai

Role = Literal["system", "user", "assistant", "tool"]
StopReason = Literal["end_turn", "tool_use", "max_tokens", "other"]


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class ToolSpec:
    """A tool as offered to the model. `parameters` is a JSON Schema object."""

    name: str
    description: str
    parameters: dict[str, Any]


@dataclass
class LLMResponse:
    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    stop_reason: StopReason = "end_turn"

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


class LLMError(RuntimeError):
    """Provider call failed in a way the loop should surface rather than retry blindly."""


# ── Message helpers ────────────────────────────────────────────────────────────
# The loop builds messages with these rather than raw dicts, so the shape stays consistent.

def system_message(content: str) -> dict[str, Any]:
    return {"role": "system", "content": content}


def user_message(content: str) -> dict[str, Any]:
    return {"role": "user", "content": content}


def assistant_message(content: str, tool_calls: list[ToolCall] | None = None) -> dict[str, Any]:
    msg: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)},
            }
            for tc in tool_calls
        ]
    return msg


def tool_message(tool_call_id: str, name: str, content: str) -> dict[str, Any]:
    return {"role": "tool", "tool_call_id": tool_call_id, "name": name, "content": content}


# ── OpenAI ─────────────────────────────────────────────────────────────────────

class OpenAIClient:
    def __init__(self, api_key: str, model: str):
        self._client = openai.AsyncOpenAI(api_key=api_key)
        self.model = model

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[ToolSpec] | None = None,
        max_tokens: int = 4096,
        temperature: float | None = None,
    ) -> LLMResponse:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_completion_tokens": max_tokens,
        }
        if temperature is not None:
            kwargs["temperature"] = temperature
        if tools:
            kwargs["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters,
                    },
                }
                for t in tools
            ]

        try:
            resp = await self._client.chat.completions.create(**kwargs)
        except openai.APIError as exc:
            raise LLMError(f"OpenAI: {exc}") from exc

        choice = resp.choices[0]
        calls = [
            ToolCall(id=tc.id, name=tc.function.name, arguments=_loads(tc.function.arguments))
            for tc in (choice.message.tool_calls or [])
        ]

        return LLMResponse(
            content=choice.message.content or "",
            tool_calls=calls,
            prompt_tokens=resp.usage.prompt_tokens if resp.usage else 0,
            completion_tokens=resp.usage.completion_tokens if resp.usage else 0,
            stop_reason=_OPENAI_STOP.get(choice.finish_reason or "", "other"),
        )


    async def stream_chat(
        self,
        messages: list[dict[str, Any]],
        max_tokens: int = 4096,
    ) -> AsyncIterator[str]:
        try:
            stream = await self._client.chat.completions.create(
                model=self.model,
                messages=messages,
                max_completion_tokens=max_tokens,
                stream=True,
            )
            async for chunk in stream:
                delta = chunk.choices[0].delta.content if chunk.choices else None
                if delta:
                    yield delta
        except openai.APIError as exc:
            raise LLMError(f"OpenAI: {exc}") from exc


_OPENAI_STOP: dict[str, StopReason] = {
    "stop": "end_turn",
    "tool_calls": "tool_use",
    "length": "max_tokens",
}


# ── Anthropic ──────────────────────────────────────────────────────────────────

class AnthropicClient:
    def __init__(self, api_key: str, model: str):
        self._client = anthropic.AsyncAnthropic(api_key=api_key)
        self.model = model

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[ToolSpec] | None = None,
        max_tokens: int = 4096,
        temperature: float | None = None,
    ) -> LLMResponse:
        system, converted = _to_anthropic_messages(messages)

        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": converted,
            "max_tokens": max_tokens,
        }
        if system:
            kwargs["system"] = system
        if temperature is not None:
            kwargs["temperature"] = temperature
        if tools:
            kwargs["tools"] = [
                {"name": t.name, "description": t.description, "input_schema": t.parameters}
                for t in tools
            ]

        try:
            resp = await self._client.messages.create(**kwargs)
        except anthropic.APIError as exc:
            raise LLMError(f"Anthropic: {exc}") from exc

        text_parts: list[str] = []
        calls: list[ToolCall] = []
        for block in resp.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                calls.append(ToolCall(id=block.id, name=block.name, arguments=dict(block.input)))

        return LLMResponse(
            content="".join(text_parts),
            tool_calls=calls,
            prompt_tokens=resp.usage.input_tokens,
            completion_tokens=resp.usage.output_tokens,
            stop_reason=_ANTHROPIC_STOP.get(resp.stop_reason or "", "other"),
        )


    async def stream_chat(
        self,
        messages: list[dict[str, Any]],
        max_tokens: int = 4096,
    ) -> AsyncIterator[str]:
        system, converted = _to_anthropic_messages(messages)
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": converted,
            "max_tokens": max_tokens,
        }
        if system:
            kwargs["system"] = system
        try:
            async with self._client.messages.stream(**kwargs) as stream:
                async for text in stream.text_stream:
                    yield text
        except anthropic.APIError as exc:
            raise LLMError(f"Anthropic: {exc}") from exc


_ANTHROPIC_STOP: dict[str, StopReason] = {
    "end_turn": "end_turn",
    "tool_use": "tool_use",
    "max_tokens": "max_tokens",
    "stop_sequence": "end_turn",
}


def _to_anthropic_messages(
    messages: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]]]:
    """Convert OpenAI-shaped messages to Anthropic's, pulling the system prompt out.

    Consecutive tool results must be merged into a single user message: Anthropic rejects
    two user messages in a row, and a parallel tool call produces exactly that.
    """
    system_parts: list[str] = []
    out: list[dict[str, Any]] = []

    for msg in messages:
        role = msg["role"]

        if role == "system":
            system_parts.append(msg["content"])

        elif role == "user":
            out.append({"role": "user", "content": msg["content"]})

        elif role == "assistant":
            blocks: list[dict[str, Any]] = []
            if msg.get("content"):
                blocks.append({"type": "text", "text": msg["content"]})
            for tc in msg.get("tool_calls", []):
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": tc["id"],
                        "name": tc["function"]["name"],
                        "input": _loads(tc["function"]["arguments"]),
                    }
                )
            if blocks:
                out.append({"role": "assistant", "content": blocks})

        elif role == "tool":
            block = {
                "type": "tool_result",
                "tool_use_id": msg["tool_call_id"],
                "content": msg["content"],
            }
            # Append to the previous user message when it is also tool results.
            if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list):
                out[-1]["content"].append(block)
            else:
                out.append({"role": "user", "content": [block]})

    return "\n\n".join(system_parts), out


# ── Factory ────────────────────────────────────────────────────────────────────

LLMClient = OpenAIClient | AnthropicClient

# Last-resort fallbacks only — the builder UI uses the live /models endpoint.
# OpenAI aliases like "gpt-4o-mini" are stable; Anthropic's dated IDs (e.g.
# claude-haiku-4-5-20251001) get retired, so we use their "latest" aliases which
# Anthropic keeps pointing at the current supported version.
DEFAULT_MODELS = {
    "openai": "gpt-4o-mini",
    "anthropic": "claude-haiku-4-5",
}


def build_client(provider: str, api_key: str, model: str = "") -> LLMClient:
    """`provider` is a ConnectorType value — "openai" or "anthropic"."""
    resolved = model or DEFAULT_MODELS.get(provider, "")
    if not resolved:
        raise LLMError(f"No model specified and no default for provider {provider!r}")

    if provider == "openai":
        return OpenAIClient(api_key, resolved)
    if provider == "anthropic":
        return AnthropicClient(api_key, resolved)
    raise LLMError(f"{provider!r} is not a language model provider")


def _loads(raw: str | None) -> dict[str, Any]:
    """Tool arguments arrive as a JSON string and models occasionally emit malformed JSON.

    Returning the raw text under an error key lets the loop feed the problem back to the
    model as a tool result instead of crashing the run.
    """
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {"__malformed_arguments__": raw}
    return parsed if isinstance(parsed, dict) else {"value": parsed}

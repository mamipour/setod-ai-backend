"""Tool registry types shared by the read, write, and guide modules."""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from pydantic import BaseModel
from sqlmodel.ext.asyncio.session import AsyncSession

from app.api.mcp.auth import McpPrincipal


class ToolError(Exception):
    """A tool ran and failed. The RPC itself succeeds with isError set."""


Handler = Callable[[McpPrincipal, AsyncSession, BaseModel], Awaitable[str]]


@dataclass
class McpTool:
    name: str
    description: str
    args_model: type[BaseModel]
    handler: Handler
    write: bool = False
    destructive: bool = False
    audited: bool = False


def input_schema(tool: McpTool) -> dict:
    schema = tool.args_model.model_json_schema()
    schema.pop("title", None)
    return schema


def annotations(tool: McpTool) -> dict:
    return {
        "readOnlyHint": not tool.write,
        "destructiveHint": tool.destructive,
        "idempotentHint": not tool.write,
        "openWorldHint": False,
    }

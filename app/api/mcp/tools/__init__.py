"""Assembled tool registry. Importing this loads every tool module."""
from app.api.mcp.tools.guide import TOOLS as _GUIDE
from app.api.mcp.tools.read import TOOLS as _READ
from app.api.mcp.tools.write import TOOLS as _WRITE

REGISTRY = {**_READ, **_GUIDE, **_WRITE}

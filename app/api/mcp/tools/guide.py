"""Guide tools. The knowledge lives in app/docs/guide and is always current."""
from __future__ import annotations

from pydantic import BaseModel

from app.api.mcp.tools.base import McpTool, ToolError
from app.docs.guide import GUIDE_VERSION, SECTIONS, load_guide

# Bump only when SKILL.md itself must change (a new required step, a renamed tool).
SKILL_MIN_VERSION = 1


class GuideArgs(BaseModel):
    section: str = "all"
    skill_version: int | None = None


async def _get_guide(principal, session, args: GuideArgs) -> str:
    if args.section != "all" and args.section not in SECTIONS:
        valid = ", ".join(["all", *SECTIONS])
        raise ToolError(f"Unknown section {args.section!r}. Valid sections: {valid}")
    body = load_guide(args.section)
    lines = [f"<!-- guide_version: {GUIDE_VERSION} -->"]
    if args.skill_version is not None and args.skill_version < SKILL_MIN_VERSION:
        lines.append(
            f"> Your setod-fde skill is v{args.skill_version}; current is v{SKILL_MIN_VERSION}. "
            "Run `npx skills update` (or /plugin update setod)."
        )
    lines.append(body)
    return "\n".join(lines)


TOOLS = {
    "setod_get_guide": McpTool(
        name="setod_get_guide",
        description=(
            "The Setod guide: what each tool returns, how memory and dedup work, when to use a "
            "code skill, and the known failure patterns. Call section 'all' once per session "
            "before building or diagnosing. Pass skill_version from the setod-fde skill."
        ),
        args_model=GuideArgs,
        handler=_get_guide,
    ),
}

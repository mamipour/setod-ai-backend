"""Setod guide: one markdown set, read by the copilot and by the MCP server."""
from pathlib import Path

_DIR = Path(__file__).parent
SECTIONS = {
    "platform": "00_platform.md",
    "tools": "10_tools.md",
    "approvals_skills": "20_approvals_skills.md",
    "code_skills": "30_code_skills.md",
    "diagnosis": "40_diagnosis.md",
    "url_monitoring": "50_url_monitoring.md",
    "connectors_missing": "60_connectors_missing.md",
    "prompt_rules": "70_prompt_rules.md",
    "failure_patterns": "80_failure_patterns.md",
}
# Bump when a file changes in a way a connected client must notice.
GUIDE_VERSION = 1


def load_guide(section: str = "all") -> str:
    if section == "all":
        return "\n\n".join(
            (_DIR / name).read_text(encoding="utf-8").strip() for name in SECTIONS.values()
        )
    if section not in SECTIONS:
        raise KeyError(section)
    return (_DIR / SECTIONS[section]).read_text(encoding="utf-8").strip()

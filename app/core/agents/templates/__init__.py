"""The template gallery."""

from app.core.agents.templates.base import Template
from app.core.agents.templates.support_triage import TEMPLATE as support_triage
from app.core.agents.templates.telegram_lead_finder import TEMPLATE as telegram_lead_finder

TEMPLATES: tuple[Template, ...] = (support_triage, telegram_lead_finder)

BY_KEY: dict[str, Template] = {t.key: t for t in TEMPLATES}


def get(key: str) -> Template | None:
    return BY_KEY.get(key)


__all__ = ["BY_KEY", "TEMPLATES", "Template", "get"]

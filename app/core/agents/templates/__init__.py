"""The template gallery."""

from app.core.agents.templates.base import CATEGORIES, Template
from app.core.agents.templates.telegram_lead_finder import TEMPLATE as telegram_lead_finder
from app.core.agents.templates.emergency_email_triage import TEMPLATE as emergency_email_triage
from app.core.agents.templates.instagram_assistant import TEMPLATE as instagram_assistant
from app.core.agents.templates.meeting_sms_reminder import TEMPLATE as meeting_sms_reminder
from app.core.agents.templates.canadian_tender_sniper import TEMPLATE as canadian_tender_sniper

TEMPLATES: tuple[Template, ...] = (
    telegram_lead_finder,
    emergency_email_triage,
    instagram_assistant,
    meeting_sms_reminder,
    canadian_tender_sniper,
)

BY_KEY: dict[str, Template] = {t.key: t for t in TEMPLATES}


def get(key: str) -> Template | None:
    return BY_KEY.get(key)


__all__ = ["BY_KEY", "CATEGORIES", "TEMPLATES", "Template", "get"]

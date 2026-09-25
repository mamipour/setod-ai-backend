"""The template gallery."""

from app.core.agents.templates.base import CATEGORIES, Template
from app.core.agents.templates.support_triage import TEMPLATE as support_triage
from app.core.agents.templates.telegram_lead_finder import TEMPLATE as telegram_lead_finder
from app.core.agents.templates.inbox_digest import TEMPLATE as inbox_digest
from app.core.agents.templates.invoice_chaser import TEMPLATE as invoice_chaser
from app.core.agents.templates.listing_monitor import TEMPLATE as listing_monitor
from app.core.agents.templates.order_confirmation import TEMPLATE as order_confirmation
from app.core.agents.templates.review_request import TEMPLATE as review_request

TEMPLATES: tuple[Template, ...] = (
    support_triage,
    telegram_lead_finder,
    inbox_digest,
    invoice_chaser,
    listing_monitor,
    order_confirmation,
    review_request,
)

BY_KEY: dict[str, Template] = {t.key: t for t in TEMPLATES}


def get(key: str) -> Template | None:
    return BY_KEY.get(key)


__all__ = ["BY_KEY", "CATEGORIES", "TEMPLATES", "Template", "get"]

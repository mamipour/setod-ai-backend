"""Missed Call Recovery — texts back the callers a small business could not answer."""

from app.core.agents.templates.base import LIMITS, Template
from app.db.models import ConnectorType, TriggerType

TEMPLATE = Template(
    key="missed_call",
    name="Missed Call Recovery",
    icon="phone-missed",
    category="Sales & leads",
    tagline="Texts back anyone whose call you missed, before they call a competitor.",
    description=(
        "Every missed call is a customer deciding whether to try you again or try someone "
        "else. This agent sends a short text within minutes, asks what they needed, and "
        "tells you what came back."
    ),
    required_connectors=(ConnectorType.twilio,),
    optional_connectors=(ConnectorType.telegram_bot,),
    trigger_type=TriggerType.schedule,
    schedule_preset="every_15_minutes",
    instructions=f"""You follow up on calls the business missed.

When you run, check for missed calls that have not been followed up yet. For each one, send a
single SMS to the caller.

The text must:
1. Apologise briefly for missing the call — one short sentence, no grovelling.
2. Say who the business is.
3. Ask what they needed, and say they can reply to this text.

Keep it under 300 characters. Write it the way a person would text, not the way a company
writes marketing copy. No emoji, no exclamation marks, no "we value your business".

If the caller has already been texted about this call, skip them.

After you finish, if the owner has Telegram connected, send them one summary message saying
how many callers you texted. If there were none, say nothing at all — do not send a message
just to report that there was nothing to do.

{LIMITS}""",
)

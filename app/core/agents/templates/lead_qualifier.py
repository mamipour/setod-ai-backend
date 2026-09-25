"""Lead Qualifier — asks new enquiries the questions a salesperson would ask first."""

from app.core.agents.templates.base import LIMITS, Template
from app.db.models import ConnectorType, TriggerType

TEMPLATE = Template(
    key="lead_qualifier",
    name="Lead Qualifier",
    icon="user-search",
    category="Sales & leads",
    tagline="Asks every new enquiry the three questions you'd ask anyway, before you call back.",
    description=(
        "When someone enquires, this agent replies and asks what they need, when they need "
        "it, and roughly what budget they have in mind. By the time you call, you already "
        "know whether it is worth the call."
    ),
    required_connectors=(ConnectorType.gmail,),
    optional_connectors=(ConnectorType.telegram_bot, ConnectorType.twilio),
    trigger_type=TriggerType.schedule,
    schedule_preset="hourly",
    instructions=f"""You qualify new enquiries so the owner knows which ones to call first.

When you run, look for new enquiries you have not already handled. For each one, send a
single reply that:
1. Thanks them for getting in touch, in one line.
2. Asks at most three questions:
   - What exactly do they need?
   - When do they need it by?
   - Do they have a budget in mind?
3. Says someone will follow up once they answer.

Ask all three in one message. Do not send a series of messages, and do not ask anything they
have already told you — if their enquiry already says what they need, ask only the other two.

Then send the owner one Telegram summary: who enquired, what you can already tell about what
they want, and whether it looks worth calling back today.

Judge "worth calling today" on urgency and specificity, not on how polite the message was. A
one-line message asking for a quote this week matters more than a long friendly one with no
timeline.

{LIMITS}""",
)

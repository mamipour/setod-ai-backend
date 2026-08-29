"""After Hours Responder — answers messages that arrive when nobody is working."""

from app.core.agents.templates.base import LIMITS, Template
from app.db.models import ConnectorType, TriggerType

TEMPLATE = Template(
    key="after_hours",
    name="After Hours Responder",
    icon="moon",
    tagline="Answers messages that arrive at night, so nobody waits until morning wondering.",
    description=(
        "Replies to anyone who messages outside business hours: confirms you got it, says "
        "when they will hear back, and answers the simple questions straight away. Anything "
        "it cannot answer, it flags for the morning."
    ),
    required_connectors=(ConnectorType.telegram_bot,),
    optional_connectors=(ConnectorType.twilio, ConnectorType.gmail),
    # Ideally this fires the moment a message arrives, but channel triggers are disabled for
    # now (they need a public webhook URL). Polling every 15 minutes is close enough for
    # after-hours traffic, and the idempotency ledger keeps each message answered exactly once.
    trigger_type=TriggerType.schedule,
    schedule_preset="every_15_minutes",
    instructions=f"""You answer messages that arrive outside business hours.

For each unread message, reply once. Your reply must:
1. Confirm a person will read it.
2. Say when — "someone will get back to you in the morning" is enough unless the instructions
   below give specific hours.
3. Answer the question directly, but only if it is one of the ones listed below.

Questions you may answer:
- Opening hours
- Where the business is located
- Whether you are open on a given day

Everything else — pricing, availability, order status, complaints, anything specific to that
person's situation — gets the holding reply and nothing more. Do not guess. Do not offer to
check. Say a person will follow up.

Keep replies to two or three sentences. Write like a person who is briefly awake, not like an
autoresponder.

{LIMITS}""",
)

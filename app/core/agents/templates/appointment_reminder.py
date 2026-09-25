"""Appointment Reminder — texts people the day before, so they turn up."""

from app.core.agents.templates.base import LIMITS, Template
from app.db.models import ConnectorType, TriggerType

TEMPLATE = Template(
    key="appointment_reminder",
    name="Appointment Reminder",
    icon="calendar-clock",
    category="Customer communication",
    tagline="Texts tomorrow's appointments today, so fewer people forget to show up.",
    description=(
        "Sends a short reminder the day before each appointment and asks people to reply if "
        "they need to change it. A no-show costs a whole slot; a text costs a cent."
    ),
    required_connectors=(ConnectorType.twilio,),
    optional_connectors=(ConnectorType.gmail, ConnectorType.telegram_bot),
    trigger_type=TriggerType.schedule,
    schedule_preset="daily_9am",
    instructions=f"""You remind people about their appointments.

When you run, find the appointments happening tomorrow. Text each person once.

The text must include:
1. The day and time of their appointment.
2. Where it is, if the business has more than one location.
3. A line telling them to reply to this message if they need to reschedule or cancel.

Keep it under 200 characters. No greeting card language — people are reading this on a lock
screen.

Text each person only once, even if they have two appointments tomorrow. In that case, put
both in the same message.

If someone has already been reminded about tomorrow's appointment, skip them.

{LIMITS}""",
)

"""Meeting SMS Reminder — checks Google Calendar and texts you before upcoming meetings."""

from app.core.agents.templates.base import Template
from app.db.models import ConnectorType, TriggerType

TEMPLATE = Template(
    key="meeting_sms_reminder",
    name="Meeting SMS Reminder",
    icon="calendar",
    category="Operations",
    tagline="Texts you before each meeting so you never walk in unprepared.",
    description=(
        "Checks your Google Calendar every 30 minutes for meetings starting in the next 2 hours "
        "and sends you an SMS with the meeting title and exact time. Remembers which meetings "
        "it has already notified you about so you never get duplicate texts."
    ),
    required_connectors=(ConnectorType.gmail, ConnectorType.twilio),
    trigger_type=TriggerType.schedule,
    schedule_preset="every_30_minutes",
    instructions="""Check the calendar for any meetings starting in the next 2 hours and send an SMS to [YOUR_PHONE_NUMBER] with the meeting title and exact date and time. Do not send an SMS if you have already sent one for this meeting.""",
)

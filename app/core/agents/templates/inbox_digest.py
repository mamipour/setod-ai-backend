"""Daily Inbox Digest — summarises every new email into a morning briefing."""

from app.core.agents.templates.base import LIMITS, Template
from app.db.models import ConnectorType, TriggerType

TEMPLATE = Template(
    key="inbox_digest",
    name="Daily Inbox Digest",
    icon="mail",
    category="Operations",
    tagline="One crisp briefing every morning — what arrived, what needs a reply, what can wait.",
    description=(
        "Reads all unread emails, groups them by urgency, and sends you a single Telegram "
        "or Slack message with a one-line summary of each thread. No inbox required."
    ),
    required_connectors=(ConnectorType.gmail,),
    optional_connectors=(ConnectorType.telegram_bot, ConnectorType.slack_webhook),
    default_tools={
        "gmail": ["read_unread_emails"],
        "telegram_bot": ["send_telegram_message"],
        "slack_webhook": ["post_to_slack"],
    },
    trigger_type=TriggerType.schedule,
    schedule_preset="daily_9am",
    instructions="""You are a personal email assistant who writes a morning briefing.

When run, read all unread emails from the last 24 hours.

Group them into two sections:
1. **Needs a reply** — questions, requests, approvals, or anything addressed directly to the owner.
2. **FYI** — newsletters, receipts, notifications, and anything that needs no action.

For each email write exactly one line:
  [sender name] — [subject] — [one sentence: what it's about and (for needs-reply) what action to take]

Format the briefing like this:

📬 Morning briefing — [date]

**Needs a reply (N)**
• …
• …

**FYI (M)**
• …
• …

Send it to Telegram or Slack (whichever is connected).
If there are no emails, send: "📬 No new emails since yesterday."

""" + LIMITS,
)

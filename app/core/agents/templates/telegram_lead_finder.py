"""Telegram Group Lead Finder — monitors group messages and alerts on qualifying leads."""

from app.core.agents.templates.base import Template
from app.db.models import ConnectorType, TriggerType

TEMPLATE = Template(
    key="telegram_lead_finder",
    name="Telegram Group Lead Finder",
    icon="search",
    category="Sales & leads",
    tagline="Watches your Telegram groups and texts you the moment someone is looking for your service.",
    description=(
        "Reads every new message in your Telegram groups since its last run and identifies "
        "people actively seeking a service provider. Sends a structured SMS lead alert — "
        "group name, sender, and a short snippet — and stays silent when there is nothing relevant."
    ),
    required_connectors=(ConnectorType.telegram_client, ConnectorType.twilio),
    trigger_type=TriggerType.schedule,
    schedule_preset="every_15_minutes",
    instructions="""Monitor Telegram group chats for people clearly seeking [YOUR SERVICE — e.g. "catering / food-service vendors"] and send a single compact SMS alert when leads appear.

Lead matching (match ANY of the positive signals; require intent + service context for borderline cases)
- Explicit intent words (looking for, need, seeking, want, hire, need a vendor) combined with [YOUR SERVICE TERMS — e.g. "cater, catering, caterer, food service, banquet, buffet, meal service"].
- Requests for recommendations or referrals for [YOUR SERVICE TYPE].
- Event contexts implying [YOUR SERVICE TYPE]: [e.g. "wedding, corporate event, conference, birthday, office lunch, team lunch, banquet"].

Negative filters (exclude)
- Self-promotion or posts clearly selling a service, bots, forwarded promotional posts, jokes/sarcasm, "not looking/no longer need," job postings for staff, dine-in/takeout requests.

Extraction (for each matching message)
- Chat name, message timestamp, sender handle/name
- Event type (if present), event date/time (if present), city/location (if present), headcount, budget, contact method
- A 12–18 word snippet capturing the request
- Message link if provided by the read tool

Prioritization
- Prefer explicit intent first; then event-context leads. If many matches, rank by explicitness and recency and report the top 3.

Actions (SMS — one message per run)
- If zero leads: do nothing.
- If ≥1 lead: send ONE SMS to [YOUR_PHONE_NUMBER] summarizing up to 3 fresh leads; if more than 3, append "(+X more)".
- SMS format (aim ≤320 chars total):
  LeadFinder: [Chat] [Sender] — [Snippet] ([When]). [Link if available] • [repeat for up to 3]
- Include only sender handle/name and the short snippet; do not include additional personal information.""",
)

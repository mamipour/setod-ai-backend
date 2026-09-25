"""Listing Monitor — tracks search results and alerts on new matches."""

from app.core.agents.templates.base import LIMITS, Template
from app.db.models import ConnectorType, TriggerType

TEMPLATE = Template(
    key="listing_monitor",
    name="Listing Monitor",
    icon="search",
    category="Sales & leads",
    tagline="Watches the web for new listings, job posts, or products matching your criteria.",
    description=(
        "Searches the web for a query you define, compares results to what it already knows "
        "about, and sends you an alert only when something new appears. Adapt it for "
        "real-estate listings, job postings, competitor products, or news."
    ),
    required_connectors=(),
    optional_connectors=(ConnectorType.telegram_bot, ConnectorType.slack_webhook, ConnectorType.gmail),
    default_tools={
        "telegram_bot": ["send_telegram_message"],
        "slack_webhook": ["post_to_slack"],
        "gmail": ["send_email"],
    },
    trigger_type=TriggerType.schedule,
    schedule_preset="every_6_hours",
    instructions="""You are a listing monitor. Your job is to search the web for new items matching a query.

## What to search for
REPLACE THIS: Describe what you want to monitor.
Examples:
  - "2-bedroom apartments for rent in downtown Toronto under $2000"
  - "senior Python engineer remote job postings"
  - "iPhone 16 Pro Max under $900 on eBay"

## Steps
1. Search the web for the query above. Get the most recent 10 results.
2. Check your memory for URLs you have already reported.
3. For each result you have NOT seen before:
   a. Write a one-line summary: [title] — [price or key detail if any] — [URL]
   b. Mark the URL as seen in memory.
4. If there are new results, send all of them in a single message to Telegram, Slack, or email (whichever is connected).
5. If nothing is new, send nothing.

Message format:
🔔 [N] new listing(s) for "[your search query]"

• [title] — [key detail] — [URL]
• …

""" + LIMITS,
)

"""Review Responder — replies to Google Business Profile reviews automatically for positives,
and escalates negatives to the owner before touching them."""

from app.core.agents.templates.base import LIMITS, Template
from app.db.models import ConnectorType, TriggerType

TEMPLATE = Template(
    key="review_responder",
    name="Review Responder",
    icon="star",
    category="Customer communication",
    tagline="Replies to good reviews, and hands you the bad ones before anything is said.",
    description=(
        "Monitors your Google Business Profile reviews on a schedule. Positive reviews get "
        "a warm, personalised reply — not a template. Negative reviews are never answered "
        "automatically: it drafts a reply and sends it to you for approval. "
        "Requires Google Business Profile and optionally Telegram for escalation."
    ),
    required_connectors=(ConnectorType.google_business_profile,),
    optional_connectors=(ConnectorType.telegram_bot,),
    trigger_type=TriggerType.schedule,
    schedule_preset="daily_9am",
    default_tools={
        "google_business_profile": [
            "list_gbp_locations",
            "list_gbp_reviews",
            "reply_to_gbp_review",
        ],
        "telegram_bot": ["send_telegram_message"],
    },
    instructions=f"""You handle customer reviews for a local business.

When you run:
1. Call list_gbp_locations to find the location id (there is usually only one).
2. Call list_gbp_reviews with a limit of 20. Each review shows whether a reply already exists.
3. Skip reviews that already have a reply — they have been handled.

For the remaining unanswered reviews, sort by sentiment:

**Positive reviews (4-5 stars, or clearly happy tone):**
Reply directly using reply_to_gbp_review. Thank the reviewer and reference something
specific they mentioned. If they praised the fast delivery, mention it. A generic
"Thank you for your review!" is worse than no reply — everyone can see it and it reads
as automated. Two sentences maximum. No ask to come back, no discounts, no links.

**Negative or mixed reviews (3 stars or below, or an unhappy tone regardless of stars):**
Do NOT reply. Instead, send the owner a Telegram message (if connected) with:
- The reviewer's name and star rating
- What they said, in one sentence
- A suggested reply they could send

Make it clear you have NOT replied. An angry customer answered badly in public is the
most expensive mistake this agent could make, so that decision belongs to a human.

**Reviews with no text, just a rating:**
Skip them. There is nothing personalised to respond to.

{LIMITS}""",
)

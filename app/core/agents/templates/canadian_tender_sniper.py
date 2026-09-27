"""Canadian Tender Sniper — monitors CanadaBuys for new construction tenders."""

from app.core.agents.templates.base import Template
from app.db.models import ConnectorType, TriggerType

TEMPLATE = Template(
    key="canadian_tender_sniper",
    name="Canadian Tender Sniper",
    icon="file-search",
    category="Sales & leads",
    tagline="Monitors CanadaBuys daily and alerts you the moment new construction tenders are posted.",
    description=(
        "Downloads the CanadaBuys open tender CSV, filters for construction (CNST) "
        "opportunities published in the last 7 days, and skips anything already reported. "
        "Sends you an SMS summary and a full-detail email for every new tender found. "
        "Silent when there is nothing new."
    ),
    required_connectors=(ConnectorType.twilio, ConnectorType.gmail),
    trigger_type=TriggerType.schedule,
    schedule_preset="every_day",
    instructions="""Monitor CanadaBuys for new construction tenders published in the last 7 days and send a single consolidated alert when new ones are found.

The tender data is at:
https://canadabuys.canada.ca/opendata/pub/openTenderNotice-ouvertAvisAppelOffres.csv

Each run:
1. Download the CSV and find tenders where the procurement category field contains "CNST" and the publication date is within the last 7 days.
2. Skip any tender you already reported in a previous run.
3. If nothing new, stop quietly — no messages.
4. If there are new tenders, send one SMS to [YOUR_PHONE_NUMBER]: how many found, up to 3 with a short title and closing date, and a note if there are more.
5. Also send one email to [YOUR_EMAIL] with full details for every new tender: title, reference number, the organisation, published and closing dates, a link, and a brief description.
6. Remember every tender you just reported so you never send the same one twice.
7. Finish with a short summary of what you found and sent.""",
)

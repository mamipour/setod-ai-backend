"""Review Request — reads a sheet of recent appointments and asks happy clients for a review."""

from app.core.agents.templates.base import LIMITS, Template
from app.db.models import ConnectorType, TriggerType

TEMPLATE = Template(
    key="review_request",
    name="Review Request",
    icon="star",
    tagline="Automatically asks satisfied clients for a review 24 hours after their appointment.",
    description=(
        "Reads a Google Sheet of recent appointments, finds ones completed yesterday that "
        "haven't been asked for a review yet, and sends a personalised email asking for one. "
        "Updates the sheet so no one is asked twice."
    ),
    required_connectors=(ConnectorType.google_sheets, ConnectorType.gmail),
    optional_connectors=(),
    default_tools={
        "google_sheets": ["read_rows", "update_cell"],
        "gmail": ["send_email"],
    },
    trigger_type=TriggerType.schedule,
    schedule_preset="daily_9am",
    instructions="""You are a client-relations assistant who requests reviews after appointments.

Your spreadsheet has these columns (starting at row 2):
A: Client name
B: Client email
C: Appointment date (YYYY-MM-DD)
D: Service type (e.g. "Consultation", "Deep Clean", "Haircut")
E: Review status — blank = not yet sent, "Sent" = already asked

## Steps
1. Read the spreadsheet (Sheet1!A2:E500).
2. Find rows where:
   - Appointment date = yesterday's date
   - Review status column is blank
3. For each matching row, send a review request email.
4. After sending, update column E for that row to "Sent".

## Email format
Subject: How was your [service_type], [client_name]?

Body:
  Hi [client_name],

  Thank you for coming in yesterday. We hope your [service_type] was exactly what you needed.

  If you have a moment, we'd love to hear how it went. A quick review helps other customers
  find us and helps us keep improving.

  [REPLACE: Insert your Google review / Trustpilot / Yelp link here]

  Thank you for your support — it means a lot to us.

  [Your business name]

Send at most one email per client per row. Skip rows that already have "Sent" in column E.

""" + LIMITS,
)

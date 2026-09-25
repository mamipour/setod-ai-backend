"""Invoice Chaser — finds overdue invoices in a spreadsheet and sends polite reminders."""

from app.core.agents.templates.base import LIMITS, Template
from app.db.models import ConnectorType, TriggerType

TEMPLATE = Template(
    key="invoice_chaser",
    name="Invoice Chaser",
    icon="receipt",
    tagline="Automatically follows up on overdue invoices so you never have to chase manually.",
    description=(
        "Reads a Google Sheet with invoice data, identifies rows that are past due and "
        "unpaid, and sends a polite email reminder to each client. Marks the sheet after "
        "sending so it never double-sends."
    ),
    required_connectors=(ConnectorType.google_sheets, ConnectorType.gmail),
    optional_connectors=(ConnectorType.slack_webhook,),
    default_tools={
        "google_sheets": ["read_rows", "update_cell"],
        "gmail": ["send_email"],
        "slack_webhook": ["post_to_slack"],
    },
    trigger_type=TriggerType.schedule,
    schedule_preset="daily_9am",
    instructions="""You are an accounts-receivable assistant who follows up on unpaid invoices.

Your spreadsheet has these columns (starting at row 2):
A: Client name
B: Client email
C: Invoice number
D: Amount (with currency symbol)
E: Due date (YYYY-MM-DD)
F: Status — either "Unpaid", "Paid", or "Reminded"

Steps:
1. Read the spreadsheet (Sheet1!A2:F200).
2. For each row where Status = "Unpaid" and Due date is before today, send a reminder email.
3. After sending, update column F for that row to "Reminded".
4. If a Slack connector is available, post a summary of what you sent.

Email format:
Subject: Friendly reminder — Invoice [invoice_number] due [due_date]
Body:
  Hi [client_name],

  I hope you are well. I'm writing to follow up on invoice [invoice_number] for [amount],
  which was due on [due_date].

  If you have already sent payment, please ignore this message. If you have any questions
  or need to discuss payment arrangements, please reply to this email.

  Thank you for your business.

  [Your name]

Do not send more than one email per row per run.
If the status is "Reminded" or "Paid", skip that row entirely.

""" + LIMITS,
)

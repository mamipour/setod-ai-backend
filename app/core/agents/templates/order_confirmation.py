"""Order Confirmation — fires on an inbound webhook and logs + confirms each order."""

from app.core.agents.templates.base import LIMITS, Template
from app.db.models import ConnectorType, TriggerType

TEMPLATE = Template(
    key="order_confirmation",
    name="Order Confirmation",
    icon="package",
    tagline="Instantly confirms orders from your store and logs them to a Google Sheet.",
    description=(
        "Triggered by an inbound webhook from your e-commerce platform or form, it sends "
        "a confirmation email to the customer and appends a row to a Google Sheet for your "
        "records. Works with Shopify, WooCommerce, Typeform, and any webhook-enabled system."
    ),
    required_connectors=(ConnectorType.webhook,),
    optional_connectors=(ConnectorType.gmail, ConnectorType.google_sheets, ConnectorType.slack_webhook),
    default_tools={
        "gmail": ["send_email"],
        "google_sheets": ["append_row"],
        "slack_webhook": ["post_to_slack"],
    },
    trigger_type=TriggerType.channel,
    instructions="""You are an order-processing assistant. You run when a new order webhook arrives.

The trigger message contains the order payload as JSON. Extract:
- order_id
- customer_name
- customer_email
- items (list: name + quantity + price each)
- total
- shipping_address

## Steps
1. If Gmail is connected, send a confirmation email to the customer:
   Subject: Order confirmed — #[order_id]
   Body: Greet the customer by name, list what they ordered, confirm the total, and give
   a realistic timeline (e.g. "ships within 2 business days").

2. If Google Sheets is connected, append one row to the orders sheet:
   [order_id, customer_name, customer_email, items summary, total, today's date, "Confirmed"]

3. If Slack is connected, post a one-line alert:
   🛒 New order #[order_id] from [customer_name] — [total]

If the payload is missing required fields, send a Slack alert flagging the incomplete order
and do not send a confirmation email.

""" + LIMITS,
)

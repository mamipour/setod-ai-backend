"""Lead Logger — captures enquiries from any channel into HubSpot or Pipedrive."""

from app.core.agents.templates.base import LIMITS, Template
from app.db.models import ConnectorType, TriggerType

TEMPLATE = Template(
    key="lead_logger",
    name="Lead Logger",
    icon="clipboard-list",
    category="Sales & leads",
    tagline="Logs every enquiry into your CRM automatically — no copy-pasting.",
    description=(
        "When someone reaches out via Instagram, WhatsApp, email, or SMS, this agent "
        "extracts their name, email, and intent, finds or creates them in your CRM, "
        "and opens a deal. Works with HubSpot or Pipedrive — connect whichever you use."
    ),
    required_connectors=(),
    optional_connectors=(
        ConnectorType.hubspot,
        ConnectorType.pipedrive,
        ConnectorType.gmail,
        ConnectorType.instagram,
        ConnectorType.whatsapp,
        ConnectorType.twilio,
        ConnectorType.telegram_bot,
    ),
    trigger_type=TriggerType.channel,
    instructions=f"""You capture new sales enquiries into the CRM so nothing falls through the cracks.

When triggered by an inbound message, do the following:

1. **Extract contact info.** Pull out the sender's name (if known), email address, and phone
   number from the message and channel metadata. If the channel gives you the email (e.g.
   Gmail), use it. If not, note what you have.

2. **Find or create the contact.**
   - If you have a CRM connector, call find_hubspot_contact or find_pipedrive_person with
     their email. If found, use the existing id. If not found, create them.
   - If you cannot identify them by email, create a contact with whatever you have (name,
     phone) and note the gap.

3. **Open a deal.**
   - First call list_hubspot_pipeline_stages or list_pipedrive_stages to see valid stage ids.
   - Create a deal titled "[Sender name] — [channel]" in the first/default stage.
   - Link it to the contact you found or created.

4. **Log a note** on the contact summarising the original message — include the channel,
   what they asked for, and any urgency signals you spotted.

5. **Reply to the sender** (via the same channel) with a brief acknowledgement:
   "Thanks for reaching out — I'll make sure the right person gets back to you shortly."
   Keep it to 1–2 sentences. Do not make commitments about timelines or outcomes.

If you have neither a HubSpot nor a Pipedrive connector, skip steps 2–4 and only do the reply.

{LIMITS}""",
    default_tools={
        "hubspot": [
            "find_hubspot_contact",
            "create_hubspot_contact",
            "create_hubspot_deal",
            "log_hubspot_note",
            "list_hubspot_pipeline_stages",
        ],
        "pipedrive": [
            "find_pipedrive_person",
            "create_pipedrive_person",
            "create_pipedrive_deal",
            "log_pipedrive_activity",
            "list_pipedrive_stages",
        ],
    },
)

"""
Agent templates
===============
A template is an instruction sheet plus the accounts it needs. That is the whole idea: the
engine underneath is identical for every agent, and what distinguishes "Missed Call Recovery"
from an empty canvas is knowing what to write in the prompt and which connectors to ask for.

Three conventions run through all six.

**Instructions are written for the model, but read by the owner.** The user is about to grant
an agent the ability to text their customers, and the create flow shows this text in an
editable box. So it is plain prose with numbered rules rather than prompt-engineering
scaffolding — someone who has never seen a system prompt should be able to read it, spot the
one line they disagree with, and change it.

**Every template states its limits.** Each one ends with what the agent must not do. Left
unsaid, a model asked to reply to customers will cheerfully quote prices, promise delivery
dates, and apologise on the company's behalf.

**Optional connectors are genuinely optional.** `required_connectors` gates the create flow;
anything listed as optional enriches the agent when present. A missed-call agent needs Twilio
and nothing else, but will use Telegram to notify the owner if it happens to be connected.
"""

from dataclasses import dataclass, field
from typing import Any

from app.db.models import ConnectorType, TriggerType


@dataclass(frozen=True)
class Template:
    key: str
    name: str
    icon: str
    # One sentence, in the owner's language, for the gallery card.
    tagline: str
    # What it actually does, two or three sentences, shown on the card's back or below it.
    description: str
    instructions: str
    # Section heading on the Templates page. Free text, but keep to the handful in CATEGORIES
    # so the page does not end up with one heading per template.
    category: str = "Operations"
    required_connectors: tuple[ConnectorType, ...] = ()
    optional_connectors: tuple[ConnectorType, ...] = ()
    trigger_type: TriggerType = TriggerType.manual
    # Schedule triggers only: a key from schedule.PRESETS.
    schedule_preset: str | None = None
    settings: dict[str, Any] = field(default_factory=dict)
    # Which tools to pre-enable per connector type. An empty dict means all tools on.
    # Keyed by ConnectorType value (string) so the dataclass stays serialisation-friendly.
    default_tools: dict[str, list[str]] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "name": self.name,
            "icon": self.icon,
            "tagline": self.tagline,
            "description": self.description,
            "instructions": self.instructions,
            "category": self.category,
            "required_connectors": [c.value for c in self.required_connectors],
            "optional_connectors": [c.value for c in self.optional_connectors],
            "trigger_type": self.trigger_type.value,
            "schedule_preset": self.schedule_preset,
            "settings": self.settings,
            "default_tools": self.default_tools,
        }


# Display order of the Templates page sections. A template whose category is not listed here
# still renders — it lands after these, alphabetically.
CATEGORIES: tuple[str, ...] = ("Customer communication", "Sales & leads", "Finance", "Operations")


# Shared closing rule. Repeated in every template rather than appended automatically, because
# a template's instructions are shown to the user verbatim and silently injected text would
# make the box they are editing not match what the agent actually receives.
LIMITS = """
Rules you must never break:
- Never invent facts. If you do not know something, say so or escalate to a human.
- Never quote prices, discounts, delivery dates, or availability unless they appear in these
  instructions or in the message you are replying to.
- Never promise anything on the business's behalf.
- Never send more than one message to the same person in a single run.
""".strip()

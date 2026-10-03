"""Register all sqladmin ModelViews and assemble the Admin instance."""
from fastapi import FastAPI
from sqladmin import Admin, ModelView

from app.admin.auth import StaffAuthBackend
from app.db.models import (
    AdminAuditLog,
    Agent,
    AgentSession,
    Connector,
    Conversation,
    Invitation,
    ModelPrice,
    Addon,
    OrgAddon,
    OrgCap,
    OrgOverride,
    OrgSubscription,
    Organization,
    OrganizationMember,
    OrgTable,
    Plan,
    StripeEvent,
    UsageEvent,
    UsagePeriod,
    User,
)
from app.db.session import engine


# ── ModelViews ────────────────────────────────────────────────────────────────

class UserAdmin(ModelView, model=User):
    column_list = [User.id, User.email, User.name, User.is_staff, User.created_at]
    column_searchable_list = [User.email, User.name]
    column_sortable_list = [User.created_at, User.email]
    form_columns = [User.is_staff]    # only editable field
    can_create = False
    can_delete = False
    name = "User"
    name_plural = "Users"
    icon = "fa-solid fa-users"


class OrganizationAdmin(ModelView, model=Organization):
    column_list = [Organization.id, Organization.name, Organization.slug, Organization.created_at]
    column_searchable_list = [Organization.name, Organization.slug]
    can_create = False
    can_delete = False
    name = "Organization"
    name_plural = "Organizations"
    icon = "fa-solid fa-building"


class OrganizationMemberAdmin(ModelView, model=OrganizationMember):
    column_list = [OrganizationMember.organization_id, OrganizationMember.user_id, OrganizationMember.role, OrganizationMember.joined_at]
    can_create = False
    can_delete = False
    name = "Member"
    name_plural = "Members"
    icon = "fa-solid fa-user-group"


class ConnectorAdmin(ModelView, model=Connector):
    # Explicitly list all columns except config so the encrypted secret is never surfaced
    column_list = [Connector.id, Connector.org_id, Connector.name, Connector.type, Connector.status, Connector.created_by, Connector.created_at]
    form_columns = [Connector.status]
    can_create = False
    can_delete = False
    name = "Connector"
    name_plural = "Connectors"
    icon = "fa-solid fa-plug"


class AgentAdmin(ModelView, model=Agent):
    column_list = [Agent.id, Agent.name, Agent.status, Agent.org_id, Agent.created_at]
    column_searchable_list = [Agent.name]
    can_create = False
    can_delete = False
    name = "Agent"
    name_plural = "Agents"
    icon = "fa-solid fa-robot"


class AgentSessionAdmin(ModelView, model=AgentSession):
    column_list = [AgentSession.id, AgentSession.agent_id, AgentSession.trigger_type, AgentSession.status, AgentSession.prompt_tokens, AgentSession.completion_tokens, AgentSession.started_at]
    column_sortable_list = [AgentSession.started_at]
    can_create = False
    can_delete = False
    can_edit = False
    name = "Session"
    name_plural = "Sessions"
    icon = "fa-solid fa-clock-rotate-left"


class ConversationAdmin(ModelView, model=Conversation):
    column_list = [Conversation.id, Conversation.org_id, Conversation.channel, Conversation.peer_name, Conversation.status, Conversation.created_at]
    can_create = False
    can_delete = False
    can_edit = False
    name = "Conversation"
    name_plural = "Conversations"
    icon = "fa-solid fa-comments"


class OrgTableAdmin(ModelView, model=OrgTable):
    column_list = [OrgTable.id, OrgTable.org_id, OrgTable.name, OrgTable.slug, OrgTable.created_at]
    can_create = False
    can_delete = False
    can_edit = False
    name = "OrgTable"
    name_plural = "OrgTables"
    icon = "fa-solid fa-table"


class InvitationAdmin(ModelView, model=Invitation):
    column_list = [Invitation.id, Invitation.organization_id, Invitation.email, Invitation.role, Invitation.accepted_at, Invitation.expires_at]
    can_create = False
    can_delete = False
    can_edit = False
    name = "Invitation"
    name_plural = "Invitations"
    icon = "fa-solid fa-envelope"


class PlanAdmin(ModelView, model=Plan):
    column_list = [Plan.code, Plan.display_name, Plan.price_cad_monthly, Plan.active, Plan.sort_order]
    form_columns = [Plan.display_name, Plan.price_cad_monthly, Plan.price_cad_annual, Plan.features, Plan.limits, Plan.included, Plan.active, Plan.stripe_monthly_price_id, Plan.stripe_annual_price_id, Plan.sort_order]
    can_delete = False
    name = "Plan"
    name_plural = "Plans"
    icon = "fa-solid fa-layer-group"


class AddonAdmin(ModelView, model=Addon):
    column_list = [Addon.code, Addon.display_name, Addon.price_cad_monthly, Addon.meter, Addon.active]
    can_delete = False
    name = "Addon"
    name_plural = "Addons"
    icon = "fa-solid fa-puzzle-piece"


class OrgSubscriptionAdmin(ModelView, model=OrgSubscription):
    column_list = [OrgSubscription.org_id, OrgSubscription.plan_code, OrgSubscription.status, OrgSubscription.stripe_customer_id, OrgSubscription.current_period_end]
    form_columns = [OrgSubscription.plan_code, OrgSubscription.status]
    can_create = False
    can_delete = False
    name = "Subscription"
    name_plural = "Subscriptions"
    icon = "fa-solid fa-credit-card"


class OrgAddonAdmin(ModelView, model=OrgAddon):
    column_list = [OrgAddon.org_id, OrgAddon.addon_code, OrgAddon.created_at]
    can_delete = False
    name = "OrgAddon"
    name_plural = "OrgAddons"
    icon = "fa-solid fa-boxes-stacked"


class OrgOverrideAdmin(ModelView, model=OrgOverride):
    column_list = [OrgOverride.org_id, OrgOverride.key, OrgOverride.value, OrgOverride.reason, OrgOverride.expires_at, OrgOverride.created_at]
    form_columns = [OrgOverride.org_id, OrgOverride.key, OrgOverride.value, OrgOverride.reason, OrgOverride.expires_at]
    name = "Override"
    name_plural = "Overrides"
    icon = "fa-solid fa-shield-halved"


class OrgCapAdmin(ModelView, model=OrgCap):
    column_list = [OrgCap.org_id, OrgCap.meter, OrgCap.hard_cap, OrgCap.created_at]
    name = "Cap"
    name_plural = "Caps"
    icon = "fa-solid fa-gauge"


class ModelPriceAdmin(ModelView, model=ModelPrice):
    column_list = [ModelPrice.provider, ModelPrice.model_slug, ModelPrice.input_per_m, ModelPrice.output_per_m, ModelPrice.managed_markup, ModelPrice.active, ModelPrice.active_from]
    form_columns = [ModelPrice.provider, ModelPrice.model_slug, ModelPrice.input_per_m, ModelPrice.output_per_m, ModelPrice.audio_in_per_m, ModelPrice.audio_out_per_m, ModelPrice.managed_markup, ModelPrice.active]
    name = "ModelPrice"
    name_plural = "ModelPrices"
    icon = "fa-solid fa-dollar-sign"


class UsageEventAdmin(ModelView, model=UsageEvent):
    column_list = [UsageEvent.org_id, UsageEvent.meter, UsageEvent.quantity, UsageEvent.cost_usd, UsageEvent.billable, UsageEvent.created_at]
    column_sortable_list = [UsageEvent.created_at]
    can_create = False
    can_delete = False
    can_edit = False
    name = "UsageEvent"
    name_plural = "UsageEvents"
    icon = "fa-solid fa-chart-bar"


class UsagePeriodAdmin(ModelView, model=UsagePeriod):
    column_list = [UsagePeriod.org_id, UsagePeriod.period_start, UsagePeriod.meter, UsagePeriod.included, UsagePeriod.used, UsagePeriod.overage, UsagePeriod.updated_at]
    can_create = False
    can_delete = False
    can_edit = False
    name = "UsagePeriod"
    name_plural = "UsagePeriods"
    icon = "fa-solid fa-calendar-check"


class StripeEventAdmin(ModelView, model=StripeEvent):
    column_list = [StripeEvent.event_id, StripeEvent.processed_at]
    can_create = False
    can_delete = False
    can_edit = False
    name = "StripeEvent"
    name_plural = "StripeEvents"
    icon = "fa-brands fa-stripe"


class AdminAuditLogAdmin(ModelView, model=AdminAuditLog):
    column_list = [AdminAuditLog.staff_user_id, AdminAuditLog.action, AdminAuditLog.target_type, AdminAuditLog.target_id, AdminAuditLog.created_at]
    can_create = False
    can_delete = False
    can_edit = False
    name = "AuditLog"
    name_plural = "AuditLog"
    icon = "fa-solid fa-scroll"


# ── Factory ───────────────────────────────────────────────────────────────────

def create_admin(app: FastAPI) -> Admin:
    admin = Admin(
        app,
        engine,
        title="Setod Admin",
        authentication_backend=StaffAuthBackend(secret_key=settings.app_secret_key),
    )
    for view in [
        UserAdmin, OrganizationAdmin, OrganizationMemberAdmin, ConnectorAdmin,
        AgentAdmin, AgentSessionAdmin, ConversationAdmin, OrgTableAdmin, InvitationAdmin,
        PlanAdmin, AddonAdmin, OrgSubscriptionAdmin, OrgAddonAdmin,
        OrgOverrideAdmin, OrgCapAdmin, ModelPriceAdmin,
        UsageEventAdmin, UsagePeriodAdmin, StripeEventAdmin, AdminAuditLogAdmin,
    ]:
        admin.add_view(view)
    return admin


# Module-level instance (app reference patched in main.py via create_admin)
from app.config import settings  # noqa: E402 (re-import for settings reference)
admin: Admin | None = None

"""
Shopify integration
===================
Auth: Custom App token.
  Store domain: ``mystore.myshopify.com``
  Access token: Admin API access token (Settings → Apps → Develop apps → Install)
  Required scopes: read_orders, read_customers, read_products, write_orders

Uses GraphQL Admin API — ``POST https://{shop}/admin/api/2026-07/graphql.json``
Header: ``X-Shopify-Access-Token: {token}``

All REST-era endpoints are deprecated as of 2024; GraphQL is the only supported path.

Tools (7):
  get_shopify_order(order_id_or_number)
  list_shopify_orders(status?, limit?)
  search_shopify_customer(email_or_phone)
  list_shopify_products(query?, limit?)
  get_shopify_product(product_id_or_handle)
  add_shopify_order_note(order_id, note)
  cancel_shopify_order(order_id, reason?)

All write tools honour dry_run.
"""

from typing import Any

import httpx

from app.core.crypto import decrypt_json
from app.core.llm.client import ToolSpec
from app.integrations.base import IntegrationError, RegisteredTool, ToolContext

_API_VERSION = "2026-07"


def _url(shop_domain: str) -> str:
    domain = shop_domain.strip().lstrip("https://").lstrip("http://").rstrip("/")
    if not domain.endswith(".myshopify.com"):
        domain = domain + ".myshopify.com"
    return f"https://{domain}/admin/api/{_API_VERSION}/graphql.json"


def _headers(token: str) -> dict[str, str]:
    return {
        "X-Shopify-Access-Token": token,
        "Content-Type": "application/json",
    }


async def _gql(shop_domain: str, token: str, query: str, variables: dict | None = None) -> dict:
    body: dict[str, Any] = {"query": query}
    if variables:
        body["variables"] = variables
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(_url(shop_domain), headers=_headers(token), json=body)
    if resp.status_code == 401:
        raise IntegrationError("Invalid Shopify access token")
    if resp.status_code == 403:
        raise IntegrationError("Token lacks required scopes. Ensure the Custom App has read_orders, read_customers, read_products, write_orders.")
    if resp.status_code != 200:
        raise IntegrationError(f"Shopify error {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    errors = data.get("errors")
    if errors:
        msgs = "; ".join(e.get("message", str(e)) for e in errors)
        raise IntegrationError(f"Shopify GraphQL error: {msgs}")
    return data.get("data", {})


async def test_connection(shop_domain: str, token: str) -> str:
    data = await _gql(shop_domain, token, "{ shop { name plan { displayName } } }")
    shop = data.get("shop", {})
    name = shop.get("name", "")
    plan = shop.get("plan", {}).get("displayName", "")
    return f"Connected to '{name}' ({plan})"


def build_tools(ctx: ToolContext) -> list[RegisteredTool]:  # noqa: C901
    raw = decrypt_json(ctx.connector.config)
    shop_domain: str = raw["shop_domain"]
    token: str = raw["access_token"]

    def n(base: str) -> str:
        return ctx.tool_name(base)

    def gql(query: str, variables: dict | None = None):
        return _gql(shop_domain, token, query, variables)

    # ── get_shopify_order ─────────────────────────────────────────────────────

    _ORDER_FIELDS = """
        id name displayFinancialStatus displayFulfillmentStatus
        createdAt updatedAt
        customer { displayName email phone }
        lineItems(first: 10) {
          edges { node { title quantity originalUnitPriceSet { shopMoney { amount currencyCode } } } }
        }
        shippingAddress { address1 city province country zip }
        totalPriceSet { shopMoney { amount currencyCode } }
        note tags
        fulfillments { status trackingInfo { company number url } }
    """

    def _format_order(order: dict) -> str:
        if not order:
            return "Order not found."
        customer = order.get("customer") or {}
        lines = [
            f"Order: {order.get('name')} (id: {order['id']})",
            f"Financial: {order.get('displayFinancialStatus')} | Fulfillment: {order.get('displayFulfillmentStatus')}",
            f"Customer: {customer.get('displayName', 'Guest')} | {customer.get('email', '')}",
        ]
        items = order.get("lineItems", {}).get("edges", [])
        if items:
            lines.append("Items: " + ", ".join(
                f"{e['node']['title']} x{e['node']['quantity']}" for e in items
            ))
        total = order.get("totalPriceSet", {}).get("shopMoney", {})
        lines.append(f"Total: {total.get('amount', '?')} {total.get('currencyCode', '')}")
        note = order.get("note")
        if note:
            lines.append(f"Note: {note}")
        fulfillments = order.get("fulfillments", [])
        for f in fulfillments:
            tracking = f.get("trackingInfo", [])
            for t in tracking:
                lines.append(f"Tracking: {t.get('company', '')} {t.get('number', '')} — {t.get('url', '')}")
        return "\n".join(lines)

    async def get_shopify_order(args: dict[str, Any], dry_run: bool) -> str:
        order_ref = str(args.get("order_id_or_number", "")).strip()
        if not order_ref:
            return "Error: 'order_id_or_number' is required."
        # If it looks like a number (e.g. "1001"), search by name; otherwise treat as GID
        if order_ref.isdigit():
            order_ref_str = f"#{order_ref}"
            data = await gql(
                f"""{{ orders(first: 1, query: "name:{order_ref_str}") {{
                    edges {{ node {{ {_ORDER_FIELDS} }} }} }} }}"""
            )
            edges = data.get("orders", {}).get("edges", [])
            order = edges[0]["node"] if edges else {}
        else:
            gid = order_ref if "gid://" in order_ref else f"gid://shopify/Order/{order_ref}"
            data = await gql(f"{{ order(id: \"{gid}\") {{ {_ORDER_FIELDS} }} }}")
            order = data.get("order") or {}
        return _format_order(order)

    # ── list_shopify_orders ───────────────────────────────────────────────────

    async def list_shopify_orders(args: dict[str, Any], dry_run: bool) -> str:
        status = str(args.get("status", "open")).strip().lower()
        limit = min(int(args.get("limit", 10)), 50)
        status_map = {"open": "open", "closed": "closed", "cancelled": "cancelled", "any": "any"}
        q_status = status_map.get(status, "open")
        query_str = f"status:{q_status}" if q_status != "any" else ""
        data = await gql(
            """query($first: Int!, $query: String) {
              orders(first: $first, query: $query, sortKey: CREATED_AT, reverse: true) {
                edges { node { id name displayFinancialStatus displayFulfillmentStatus createdAt
                  customer { displayName email }
                  totalPriceSet { shopMoney { amount currencyCode } } } } } }""",
            {"first": limit, "query": query_str},
        )
        edges = data.get("orders", {}).get("edges", [])
        if not edges:
            return f"No {status} orders found."
        lines = []
        for e in edges:
            o = e["node"]
            cust = o.get("customer") or {}
            total = o.get("totalPriceSet", {}).get("shopMoney", {})
            lines.append(
                f"{o['name']} | {o.get('displayFulfillmentStatus')} | "
                f"{cust.get('displayName', 'Guest')} | "
                f"{total.get('amount', '?')} {total.get('currencyCode', '')} | "
                f"id: {o['id']}"
            )
        return "\n".join(lines)

    # ── search_shopify_customer ───────────────────────────────────────────────

    async def search_shopify_customer(args: dict[str, Any], dry_run: bool) -> str:
        search = str(args.get("email_or_phone", "")).strip()
        if not search:
            return "Error: 'email_or_phone' is required."
        data = await gql(
            """query($query: String!) {
              customers(first: 5, query: $query) {
                edges { node { id displayName email phone
                  numberOfOrders totalSpentV2 { amount currencyCode }
                  orders(first: 3, sortKey: CREATED_AT, reverse: true) {
                    edges { node { name displayFulfillmentStatus createdAt } } } } } } }""",
            {"query": search},
        )
        edges = data.get("customers", {}).get("edges", [])
        if not edges:
            return f"No customer found matching '{search}'."
        lines = []
        for e in edges:
            c = e["node"]
            total = c.get("totalSpentV2", {})
            lines.append(
                f"Customer: {c.get('displayName')} | {c.get('email', '')} | {c.get('phone', '')} | "
                f"Orders: {c.get('numberOfOrders', 0)} | "
                f"Spent: {total.get('amount', '?')} {total.get('currencyCode', '')} | "
                f"id: {c['id']}"
            )
            for oe in c.get("orders", {}).get("edges", [])[:3]:
                o = oe["node"]
                lines.append(f"  → {o['name']} — {o.get('displayFulfillmentStatus')} ({o.get('createdAt', '')[:10]})")
        return "\n".join(lines)

    # ── list_shopify_products ─────────────────────────────────────────────────

    async def list_shopify_products(args: dict[str, Any], dry_run: bool) -> str:
        query = str(args.get("query", "")).strip()
        limit = min(int(args.get("limit", 10)), 50)
        data = await gql(
            """query($first: Int!, $query: String) {
              products(first: $first, query: $query) {
                edges { node { id title handle status totalInventory
                  priceRangeV2 { minVariantPrice { amount currencyCode } } } } } }""",
            {"first": limit, "query": query or None},
        )
        edges = data.get("products", {}).get("edges", [])
        if not edges:
            return f"No products found{' for ' + repr(query) if query else ''}."
        lines = []
        for e in edges:
            p = e["node"]
            price = p.get("priceRangeV2", {}).get("minVariantPrice", {})
            lines.append(
                f"• {p['title']} ({p.get('status', '')}) | "
                f"from {price.get('amount', '?')} {price.get('currencyCode', '')} | "
                f"stock: {p.get('totalInventory', '?')} | handle: {p.get('handle')} | id: {p['id']}"
            )
        return "\n".join(lines)

    # ── get_shopify_product ───────────────────────────────────────────────────

    async def get_shopify_product(args: dict[str, Any], dry_run: bool) -> str:
        ref = str(args.get("product_id_or_handle", "")).strip()
        if not ref:
            return "Error: 'product_id_or_handle' is required."
        if ref.isdigit():
            gid = f"gid://shopify/Product/{ref}"
        elif "gid://" in ref:
            gid = ref
        else:
            # handle — look up via products query
            data = await gql(
                """query($query: String!) {
                  products(first: 1, query: $query) {
                    edges { node { id } } } }""",
                {"query": f"handle:{ref}"},
            )
            edges = data.get("products", {}).get("edges", [])
            if not edges:
                return f"Product handle '{ref}' not found."
            gid = edges[0]["node"]["id"]

        data = await gql(
            """query($id: ID!) {
              product(id: $id) {
                id title handle status totalInventory description
                priceRangeV2 { minVariantPrice { amount currencyCode } maxVariantPrice { amount currencyCode } }
                variants(first: 10) {
                  edges { node { id title price inventoryQuantity sku } } } } }""",
            {"id": gid},
        )
        p = data.get("product")
        if not p:
            return "Product not found."
        price = p.get("priceRangeV2", {})
        mn = price.get("minVariantPrice", {})
        mx = price.get("maxVariantPrice", {})
        lines = [
            f"Product: {p['title']} (handle: {p.get('handle')}, id: {p['id']})",
            f"Status: {p.get('status')} | Stock: {p.get('totalInventory', '?')}",
            f"Price: {mn.get('amount')}–{mx.get('amount')} {mn.get('currencyCode', '')}",
        ]
        desc = p.get("description", "").strip()
        if desc:
            lines.append(f"Description: {desc[:300]}")
        variants = p.get("variants", {}).get("edges", [])
        if variants:
            lines.append("Variants:")
            for ve in variants:
                v = ve["node"]
                lines.append(f"  • {v['title']} — {v['price']} | qty: {v.get('inventoryQuantity', '?')} | SKU: {v.get('sku', '')}")
        return "\n".join(lines)

    # ── add_shopify_order_note ────────────────────────────────────────────────

    async def add_shopify_order_note(args: dict[str, Any], dry_run: bool) -> str:
        order_id = str(args.get("order_id", "")).strip()
        note = str(args.get("note", "")).strip()
        if not order_id or not note:
            return "Error: 'order_id' and 'note' are required."
        gid = order_id if "gid://" in order_id else f"gid://shopify/Order/{order_id}"
        if dry_run:
            return f"[simulated] Would add note to order {order_id}: {note[:80]}"
        data = await gql(
            """mutation($id: ID!, $note: String!) {
              orderUpdate(input: { id: $id, note: $note }) {
                order { id name note }
                userErrors { field message } } }""",
            {"id": gid, "note": note},
        )
        errors = data.get("orderUpdate", {}).get("userErrors", [])
        if errors:
            return "Error: " + "; ".join(e["message"] for e in errors)
        order_name = data.get("orderUpdate", {}).get("order", {}).get("name", order_id)
        return f"Note added to order {order_name}."

    # ── cancel_shopify_order ──────────────────────────────────────────────────

    async def cancel_shopify_order(args: dict[str, Any], dry_run: bool) -> str:
        order_id = str(args.get("order_id", "")).strip()
        reason = str(args.get("reason", "CUSTOMER")).strip().upper()
        if not order_id:
            return "Error: 'order_id' is required."
        gid = order_id if "gid://" in order_id else f"gid://shopify/Order/{order_id}"
        valid_reasons = {"CUSTOMER", "DECLINED", "FRAUD", "INVENTORY", "STAFF", "OTHER"}
        if reason not in valid_reasons:
            reason = "OTHER"
        if dry_run:
            return f"[simulated] Would cancel order {order_id} (reason: {reason})"
        data = await gql(
            """mutation($id: ID!, $reason: OrderCancelReason!) {
              orderCancel(orderId: $id, reason: $reason, notifyCustomer: true, refund: false, restock: false) {
                job { id }
                userErrors { field message } } }""",
            {"id": gid, "reason": reason},
        )
        errors = data.get("orderCancel", {}).get("userErrors", [])
        if errors:
            return "Error: " + "; ".join(e["message"] for e in errors)
        return f"Order {order_id} cancellation requested (reason: {reason}). Customer will be notified."

    # ── tool specs ─────────────────────────────────────────────────────────────

    return [
        RegisteredTool(
            spec=ToolSpec(
                name=n("get_shopify_order"),
                description=(
                    "Get full details for a Shopify order: status, items, customer, tracking, total, notes. "
                    "Pass the order number (e.g. '1001'), the numeric id, or the GID."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "order_id_or_number": {"type": "string", "description": "Order number (1001), numeric id, or GID."},
                    },
                    "required": ["order_id_or_number"],
                },
            ),
            handler=get_shopify_order,
        ),
        RegisteredTool(
            spec=ToolSpec(
                name=n("list_shopify_orders"),
                description=(
                    "List recent Shopify orders. Filter by status: open (default), closed, cancelled, or any. "
                    "Returns order number, fulfillment status, customer, and total."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "status": {"type": "string", "description": "Order status: open, closed, cancelled, any (default: open)."},
                        "limit": {"type": "integer", "description": "Max orders to return (default 10, max 50)."},
                    },
                    "required": [],
                },
            ),
            handler=list_shopify_orders,
        ),
        RegisteredTool(
            spec=ToolSpec(
                name=n("search_shopify_customer"),
                description=(
                    "Find a Shopify customer by email or phone number. "
                    "Returns customer profile and their 3 most recent orders."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "email_or_phone": {"type": "string", "description": "Customer email or phone number."},
                    },
                    "required": ["email_or_phone"],
                },
            ),
            handler=search_shopify_customer,
        ),
        RegisteredTool(
            spec=ToolSpec(
                name=n("list_shopify_products"),
                description=(
                    "Search the Shopify product catalogue. Optionally filter by title or tag. "
                    "Returns product name, price range, and inventory."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search term (title, tag, etc.). Leave empty to list all."},
                        "limit": {"type": "integer", "description": "Max products (default 10, max 50)."},
                    },
                    "required": [],
                },
            ),
            handler=list_shopify_products,
        ),
        RegisteredTool(
            spec=ToolSpec(
                name=n("get_shopify_product"),
                description=(
                    "Get detailed information about a single Shopify product including all variants and inventory. "
                    "Pass the product handle (from URL), numeric id, or GID."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "product_id_or_handle": {"type": "string", "description": "Product handle, numeric id, or GID."},
                    },
                    "required": ["product_id_or_handle"],
                },
            ),
            handler=get_shopify_product,
        ),
        RegisteredTool(
            spec=ToolSpec(
                name=n("add_shopify_order_note"),
                description=(
                    "Add an internal note to a Shopify order. "
                    "Notes are visible to staff in the Shopify admin only — not to customers. "
                    "Use to log support interactions, special instructions, or follow-up reminders."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "order_id": {"type": "string", "description": "Order numeric id or GID."},
                        "note": {"type": "string", "description": "Note text to add."},
                    },
                    "required": ["order_id", "note"],
                },
            ),
            handler=add_shopify_order_note,
        ),
        RegisteredTool(
            spec=ToolSpec(
                name=n("cancel_shopify_order"),
                description=(
                    "Cancel a Shopify order and notify the customer. "
                    "The order must be in a cancellable state (not already fulfilled). "
                    "Reason: CUSTOMER (default), FRAUD, INVENTORY, DECLINED, STAFF, or OTHER."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "order_id": {"type": "string", "description": "Order numeric id or GID."},
                        "reason": {"type": "string", "description": "Cancellation reason: CUSTOMER, FRAUD, INVENTORY, DECLINED, STAFF, OTHER."},
                    },
                    "required": ["order_id"],
                },
            ),
            handler=cancel_shopify_order,
        ),
    ]

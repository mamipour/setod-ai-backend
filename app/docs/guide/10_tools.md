## What each tool actually returns — read this before writing any prompt

### Cross-run deduplication — what is and isn't covered

**Covered automatically:** dedicated inbox reading tools (`read_unread_emails`, `read_telegram_messages`). These tools track what has already been seen on every call — the agent never needs to mention deduplication in the prompt for inbox use cases. If a run crashes before acting, those items come back next run rather than being lost. Write actions like reply and archive lock their item permanently the moment they fire.

**NOT covered automatically:** fetching web pages, CSV files, RSS feeds, or web search results. For these, the platform has no way to know which items the agent already acted on. The "remember past runs" toggle (under the agent's Settings tab) handles this transparently — when enabled, the platform gives the agent a running memory it updates each run. You do not need to explain memory mechanics in the prompt; just write the intent in plain English ("don't notify me about the same tender twice").

Never write memory syntax, tool call counts, or platform mechanics into the prompt — those are implementation details the platform and the model handle internally.

### Telegram Client
- **`read_telegram_messages`**: Returns the most recent unread message for each chat/group that has unread messages, up to a limit (default 10, max 25). Returns **one message per chat** — not the full chat history. Automatically skips chats whose last message was already seen in a previous run. Returns "No new unread Telegram messages since the last run." when nothing is new. The unread flag is always available — never write fallback logic for when it is missing.
- **`send_telegram_message`**: Sends a message from the user's account to any @username, phone number, or chat ID.
- **No mark-as-read tool exists.** There is no way to mark Telegram messages as read. Do not suggest it.
- **No search tool exists.** You cannot search Telegram history or filter by keyword at fetch time.

### Telegram Bot
- **`send_telegram_message`**: Sends a message to the single admin chat configured in the connector. One direction only — it cannot read anything.

### Google Gmail
- **`read_unread_emails`**: Lists inbox emails that arrived since this agent's last run, oldest first, with a body preview. Independent of the read/unread flag, so mail the owner already opened is still included. Excludes messages this agent already handled. Default 10, max 25.
- **`search_emails`**: Searches with Gmail-style syntax: `from:`, `subject:`, `after:`, `before:`, `is:unread`, `is:read`, etc. Returns up to 25 results. **Does NOT use the deduplication tracker** — always returns whatever matches the query regardless of prior runs.
- **`send_email`**: Sends a plain-text email. `to` and `body` are required, `subject` is optional.
- **`reply_to_email`**: Replies to an existing email (takes `message_id` from `read_unread_emails`). Immediately marks the email permanently processed — a second run cannot reply to the same email again.
- **`archive_email`**: Moves an email out of the inbox (not deleted). Immediately marks permanently processed.

### Google Calendar
- **`list_calendar_events`**: Lists events on the user's primary calendar between two dates (YYYY-MM-DD). Defaults to today. Returns title, id, start, end, location, and attendees. No deduplication — it's a query, not an inbox.
- **`create_calendar_event`**: Creates an event with title, start, end (YYYY-MM-DD or YYYY-MM-DDTHH:MM). Optional: description, location, attendees (comma-separated emails — each receives a Google invite), timezone (e.g. America/Toronto).

### Twilio
- **`send_sms`**: Sends SMS from the connector's fixed phone number. `to` must be E.164 format (e.g. +15551234567). Messages over 160 characters are split into multiple SMS segments and charged per segment — keep bodies under 320 characters when possible. No deduplication — each call sends a new SMS.

### Tool behaviour rules every prompt must respect
- Agents can only use tools from connectors the user has attached
- Reading tools (Gmail read, Telegram read) are safe to call freely; they automatically skip items already seen in past runs
- Writing tools (send email, send SMS, send Telegram) should fire once per run unless the prompt explicitly allows more — write this in plain English ("send one notification per run"), never reference tool call counts
- There is no file system and no code execution. Web search is a settings toggle. MCP tools only exist if the user attached an MCP connector.
- **`query_data`** exists only when the agent has a CSV or Excel file in its Knowledge tab. It runs one read-only SQL statement (DuckDB dialect) over those files as tables and returns up to 200 rows. The agent already sees every table's columns, types and a sample row in the tool description — prompts should say *what* to find ("tenders closing in the next 14 days in the IT category"), never write SQL or column names. Suggest it whenever a prompt would otherwise ask the agent to "read the file" or "go through all rows".
- **Two kinds of cross-run memory, pick the right one.** *Remember past runs* (a Settings toggle, `episodic_memory`) gives the agent fuzzy recall of what it observed and did — right for "don't notify about the same listing twice" where matching is by meaning. `memory_get` / `memory_set` / `memory_delete` / `memory_list` (on by default, setting `kv_memory`) store exact values the agent chooses to keep — the last order id it confirmed, the tender refs it already reported, how many reminders it sent someone. Keys starting with `shared:` are visible to every agent in the workspace. When a prompt needs exactness ("only process orders newer than the last one you handled", "never remind the same client more than twice"), say so in plain English and name what to remember ("keep the id of the last order you confirmed") — do not write tool syntax, JSON shapes, or key names; the agent picks those. The owner can see and edit every stored value on the agent's Memory tab, so mention that when the state matters ("you can reset the last-processed id on the Memory tab if a run goes wrong").

### Every connector tool name

Built-in names. MCP server tools are not in this list; read them off the agent.

- `airtable`: `list_airtable_bases`, `list_airtable_records`, `find_airtable_record`, `create_airtable_record`, `update_airtable_record`
- `calendar`: `list_calendar_events`, `create_calendar_event`
- `calendly`: `list_calendly_event_types`, `get_calendly_availability`, `list_calendly_events`, `get_calendly_event`, `create_calendly_booking`, `cancel_calendly_event`, `create_scheduling_link`
- `gmail`: `read_unread_emails`, `search_emails`, `send_email`, `reply_to_email`, `archive_email`
- `google_business_profile`: `list_gbp_locations`, `list_gbp_reviews`, `reply_to_gbp_review`, `delete_gbp_reply`
- `hubspot`: `find_hubspot_contact`, `create_hubspot_contact`, `update_hubspot_contact`, `create_hubspot_deal`, `move_hubspot_deal`, `log_hubspot_note`, `list_hubspot_pipeline_stages`
- `instagram`: `get_instagram_posts`, `get_instagram_comments`, `reply_to_instagram_comment`, `hide_instagram_comment`, `delete_instagram_comment`, `read_instagram_messages`, `reply_to_instagram_dm`
- `notion` (defined, but not registered — agents cannot call these): `search_notion`, `get_notion_page`, `query_notion_database`, `create_notion_page`, `update_notion_page`, `append_notion_content`
- `pipedrive`: `find_pipedrive_person`, `create_pipedrive_person`, `update_pipedrive_person`, `create_pipedrive_deal`, `move_pipedrive_deal`, `log_pipedrive_activity`, `list_pipedrive_stages`
- `sheets` (defined, but not registered — agents cannot call these): `read_rows`, `append_row`, `update_cell`
- `shopify`: `get_shopify_order`, `list_shopify_orders`, `search_shopify_customer`, `list_shopify_products`, `get_shopify_product`, `add_shopify_order_note`, `cancel_shopify_order`
- `slack`: `post_to_slack`
- `telegram`: `send_telegram_message`, `read_telegram_messages`
- `twilio`: `send_sms`
- `websearch`: `search_web`, `fetch_page`, `fetch_and_query_csv`
- `whatsapp`: `send_whatsapp_message`, `read_whatsapp_messages`
- `tables`: `{slug}_search`, `{slug}_get`, `{slug}_create`, `{slug}_update` where slug is the table's name.
- `websearch` (toggles, not a connector): `search_web`, `fetch_page`, `fetch_and_query_csv`.
- Key-value memory, on by default: `memory_get`, `memory_set`, `memory_delete`, `memory_list`.
- Knowledge files: `query_data` when the agent has a CSV or Excel file.

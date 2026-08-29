"""
Web search and page reading
===========================
Unlike every other integration, these tools are not backed by a connector — there is no
account to connect and no credential to store. They are switched on per agent in Settings,
so they are built directly from `Agent.settings` rather than from an `AgentTool` row.

Search goes through DuckDuckGo's HTML endpoint, which needs no API key. That keeps the
feature free for the SMB audience, at the cost of parsing markup that can change: a parse
that finds nothing returns "no results" to the model rather than raising, so a DuckDuckGo
layout change degrades the agent instead of breaking the run.

Fetched pages are stripped to text and truncated. A model handed 200KB of navigation markup
spends most of its context on it and answers worse than one given the first few pages of
prose.
"""

import html
import re
from typing import Any

import httpx

from app.core.agents.base import RegisteredTool
from app.core.llm.client import ToolSpec

# How many search results each context size returns. More results cost tokens on every
# search, which is why this is a setting rather than a fixed number.
CONTEXT_SIZES = {"low": 3, "medium": 5, "high": 10}

# Characters of page text handed to the model. Roughly 4k tokens — enough for an article,
# small enough that one fetch cannot eat an entire iteration's context.
PAGE_CHARS = 16_000

SEARCH_URL = "https://html.duckduckgo.com/html/"
TIMEOUT = 20

# A default user agent gets an empty page back from DuckDuckGo.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

_RESULT = re.compile(
    r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="(?P<url>[^"]+)"[^>]*>(?P<title>.*?)</a>',
    re.DOTALL | re.IGNORECASE,
)
_SNIPPET = re.compile(
    r'<a[^>]+class="[^"]*result__snippet[^"]*"[^>]*>(?P<text>.*?)</a>',
    re.DOTALL | re.IGNORECASE,
)
_DROP_BLOCKS = re.compile(
    r"<(script|style|noscript|svg|head)\b.*?</\1>", re.DOTALL | re.IGNORECASE
)
_TAG = re.compile(r"<[^>]+>")
_BLANK_LINES = re.compile(r"\n{3,}")


def build_tools(*, live_page_access: bool, context_size: str) -> list[RegisteredTool]:
    """Web tools for an agent whose `web_search` setting is on.

    `fetch_page` is withheld unless `live_page_access` is also on: reading whole pages is
    both slower and a wider door than reading search snippets, so it is opted into
    separately.
    """
    limit = CONTEXT_SIZES.get(context_size, CONTEXT_SIZES["medium"])

    tools = [
        RegisteredTool(
            spec=ToolSpec(
                name="search_web",
                description=(
                    "Search the web and get back titles, links and short snippets. "
                    "Use for facts you do not have and cannot get from the connected "
                    "accounts. Snippets are short — they answer simple questions but not "
                    "detailed ones."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "What to search for."}
                    },
                    "required": ["query"],
                },
            ),
            handler=_search_handler(limit),
        )
    ]

    if live_page_access:
        tools.append(
            RegisteredTool(
                spec=ToolSpec(
                    name="fetch_page",
                    description=(
                        "Read the text of a web page. Use after search_web when a snippet "
                        "is not enough. Returns plain text with markup removed, truncated "
                        "if the page is long."
                    ),
                    parameters={
                        "type": "object",
                        "properties": {
                            "url": {
                                "type": "string",
                                "description": "Full URL including https://.",
                            }
                        },
                        "required": ["url"],
                    },
                ),
                handler=_fetch_handler,
            )
        )

    return tools


# ── Handlers ───────────────────────────────────────────────────────────────────

def _search_handler(limit: int):
    async def handler(args: dict[str, Any], dry_run: bool) -> str:
        # Search changes nothing, so it runs for real even in a simulated run — a preview
        # built on invented search results would not tell the user anything true.
        query = str(args.get("query", "")).strip()
        if not query:
            return "Error: query is required."

        try:
            async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
                resp = await client.post(SEARCH_URL, data={"q": query}, headers=HEADERS)
                resp.raise_for_status()
        except httpx.HTTPError as exc:
            return f"Error: web search failed ({type(exc).__name__})."

        results = _parse_results(resp.text, limit)
        if not results:
            return f"No results for {query!r}."

        lines = [f"{len(results)} result(s) for {query!r}:"]
        for i, (title, url, snippet) in enumerate(results, 1):
            lines.append(f"{i}. {title}\n   {url}\n   {snippet}" if snippet else f"{i}. {title}\n   {url}")
        return "\n".join(lines)

    return handler


async def _fetch_handler(args: dict[str, Any], dry_run: bool) -> str:
    url = str(args.get("url", "")).strip()
    if not url:
        return "Error: url is required."
    if not url.startswith(("http://", "https://")):
        return "Error: url must start with http:// or https://."

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
            resp = await client.get(url, headers=HEADERS)
            resp.raise_for_status()
    except httpx.HTTPError as exc:
        return f"Error: could not fetch the page ({type(exc).__name__})."

    if "html" not in resp.headers.get("content-type", "").lower():
        return f"Error: {url} is not an HTML page."

    text = _to_text(resp.text)
    if not text:
        return f"{url} returned no readable text."
    if len(text) > PAGE_CHARS:
        return f"{text[:PAGE_CHARS]}\n\n[truncated — the page is longer than this]"
    return text


# ── Parsing ────────────────────────────────────────────────────────────────────

def _parse_results(markup: str, limit: int) -> list[tuple[str, str, str]]:
    titles = _RESULT.findall(markup)[:limit]
    snippets = [_clean(s) for s in _SNIPPET.findall(markup)[:limit]]

    out: list[tuple[str, str, str]] = []
    for i, (url, title) in enumerate(titles):
        snippet = snippets[i] if i < len(snippets) else ""
        out.append((_clean(title), html.unescape(url), snippet))
    return out


def _clean(fragment: str) -> str:
    """Strip tags and entities out of a snippet of markup."""
    return " ".join(html.unescape(_TAG.sub("", fragment)).split())


def _to_text(markup: str) -> str:
    """Crude but dependency-free HTML to text: drop non-content blocks, then all tags."""
    body = _DROP_BLOCKS.sub(" ", markup)
    body = re.sub(r"<(br|/p|/div|/li|/h[1-6])\s*/?>", "\n", body, flags=re.IGNORECASE)
    body = _TAG.sub("", body)
    body = html.unescape(body)
    lines = [" ".join(line.split()) for line in body.splitlines()]
    return _BLANK_LINES.sub("\n\n", "\n".join(line for line in lines if line)).strip()

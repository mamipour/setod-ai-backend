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

DuckDuckGo throttles per IP and says so quietly — a 200 with no rows, or a 202/403/429.
Both back off with jitter, and exhausting the budget trips a process-wide cooldown, because
the limit is on our egress address: one run that keeps retrying deepens the block for every
other run. The messages returned in that state tell the model to stop searching rather than
leaving it to discover the wall one query at a time.

Fetching accepts any text payload, not just HTML — open-data endpoints serve CSV and JSON,
often mislabelled application/octet-stream, and those are exactly the sources agents should
prefer over scraping a page. Content type is therefore a hint; the bytes decide.

HTML is reduced to text by removing page chrome (nav, header, footer, aside, forms and ARIA
landmark equivalents) from the DOM and flattening what remains, with each link kept as
"label (url)". Chrome removal is deliberate over "main content" extractors like trafilatura
or readability: those are tuned for articles and silently drop metadata sidebars — a tender's
Status field, a listing's date column — which is exactly the data agents fetch pages for.
Removing known chrome is deterministic; it can waste some tokens, never lose content.

Everything is truncated at a line boundary with a note saying how much was cut, so a model
reading a long listing knows it saw part of the page rather than assuming it saw all of it.
"""

import asyncio
import html
import random
import re
import time
from typing import Any
from urllib.parse import urljoin

import httpx
from lxml import etree
from lxml import html as lxml_html

from app.core.agents.base import RegisteredTool
from app.core.llm.client import ToolSpec

# How many search results each context size returns. More results cost tokens on every
# search, which is why this is a setting rather than a fixed number.
CONTEXT_SIZES = {"low": 3, "medium": 5, "high": 10}

# Characters of page text handed to the model. Roughly 4k tokens — enough for an article,
# small enough that one fetch cannot eat an entire iteration's context.
PAGE_CHARS = 16_000

# Stop reading a response body here. PAGE_CHARS truncates what the model sees, but without
# a byte cap an open-data CSV could pull hundreds of megabytes into memory first.
MAX_BYTES = 5 * 1024 * 1024

# Formats no amount of decoding makes useful to a model. Everything not on this list is
# downloaded and sniffed instead of trusted: open-data endpoints routinely serve CSV as
# application/octet-stream, so a content-type allowlist would reject exactly the structured
# sources agents are told to prefer.
_BINARY_TYPES = (
    "image/", "audio/", "video/", "font/",
    "pdf", "zip", "gzip", "x-tar", "msword", "officedocument", "ms-excel",
)

SEARCH_URL = "https://html.duckduckgo.com/html/"
TAVILY_SEARCH_URL = "https://api.tavily.com/search"
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
# Anchors are rewritten to "text (url)" before tags are stripped. Without this the href is
# discarded and a listing page becomes a wall of titles with no way to reach any of them.
_ANCHOR = re.compile(
    r"<a\b[^>]*?\shref=[\"'](?P<href>[^\"']*)[\"'][^>]*>(?P<label>.*?)</a>",
    re.DOTALL | re.IGNORECASE,
)
_TAG = re.compile(r"<[^>]+>")
_BLANK_LINES = re.compile(r"\n{3,}")


def build_tools(
    *,
    live_page_access: bool,
    context_size: str,
    tavily_api_key: str | None = None,
) -> list[RegisteredTool]:
    """Web tools for an agent whose `web_search` setting is on.

    `fetch_page` is withheld unless `live_page_access` is also on: reading whole pages is
    both slower and a wider door than reading search snippets, so it is opted into
    separately.
    """
    limit = CONTEXT_SIZES.get(context_size, CONTEXT_SIZES["medium"])
    provider = "tavily" if tavily_api_key else "duckduckgo"

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
            handler=_search_handler(limit, provider=provider, tavily_key=tavily_api_key),
        )
    ]

    if live_page_access:
        tools.append(
            RegisteredTool(
                spec=ToolSpec(
                    name="fetch_page",
                    description=(
                        "Read the contents of a URL. Works on web pages and on data files "
                        "such as CSV, JSON, and XML — prefer a data file over a page when "
                        "one is available, as it is cleaner and more complete. Web pages "
                        "come back as text with markup removed and each link rendered as "
                        "'label (url)', so you can follow rows through to their detail "
                        "pages. Long content is truncated."
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

# DuckDuckGo throttles per IP and signals it two ways: a 200 with no result rows, or a
# 202/403/429. Both mean back off. Retries use exponential backoff with jitter so several
# agents throttled at once do not resynchronise into another burst.
_RETRY_BASE = 1.0    # first backoff, doubled each attempt
_RETRY_BUDGET = 15.0  # give up on one query after this many cumulative seconds
_COOLDOWN = 60.0      # after giving up, short-circuit every query for this long
_RATE_LIMIT_CODES = {202, 403, 429}

# Process-wide, deliberately not per-agent: the limit is on our egress IP, so one run that
# exhausts its retries must stop every other run from queueing into the same wall. Without
# this a single agent making eight searches spends minutes retrying and deepens the block.
_blocked_until = 0.0


def _search_handler(limit: int, *, provider: str = "duckduckgo", tavily_key: str | None = None):
    async def handler(args: dict[str, Any], dry_run: bool) -> str:
        # Search changes nothing, so it runs for real even in a simulated run — a preview
        # built on invented search results would not tell the user anything true.
        query = str(args.get("query", "")).strip()
        if not query:
            return "Error: query is required."

        if provider == "tavily" and tavily_key:
            return await _tavily_search(query, limit, tavily_key)
        return await _ddg_search(query, limit)

    return handler


async def _tavily_search(query: str, limit: int, api_key: str) -> str:
    """Search via Tavily. Per-key rate limits, no IP throttling."""
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.post(
                TAVILY_SEARCH_URL,
                json={"query": query, "max_results": limit, "search_depth": "basic"},
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            )
            if resp.status_code == 401:
                return "Error: Tavily API key is invalid or expired."
            if resp.status_code == 429:
                return "Error: Tavily rate limit reached. Try again shortly."
            resp.raise_for_status()
    except httpx.HTTPError as exc:
        return f"Error: web search failed ({type(exc).__name__})."

    data = resp.json()
    items = data.get("results") or []
    if not items:
        return f"No results for {query!r}."

    results = [
        (r.get("title") or "", r.get("url") or "", (r.get("content") or "")[:200])
        for r in items[:limit]
    ]
    return _format_results(query, results)


async def _ddg_search(query: str, limit: int) -> str:
    """Search via DuckDuckGo HTML endpoint with rate-limit backoff."""
    global _blocked_until

    remaining = _blocked_until - time.monotonic()
    if remaining > 0:
        return (
            f"Web search is rate-limited right now (retry in about {int(remaining)}s). "
            "Do not keep searching — continue with what you already have, or say you "
            "could not verify this."
        )

    waited, backoff = 0.0, _RETRY_BASE
    while True:
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
                resp = await client.post(SEARCH_URL, data={"q": query}, headers=HEADERS)
            throttled = resp.status_code in _RATE_LIMIT_CODES
            if not throttled:
                resp.raise_for_status()
        except httpx.HTTPError as exc:
            return f"Error: web search failed ({type(exc).__name__})."

        results = [] if throttled else _parse_results(resp.text, limit)
        if results:
            return _format_results(query, results)

        if waited >= _RETRY_BUDGET:
            _blocked_until = time.monotonic() + _COOLDOWN
            return (
                f"Web search is rate-limited and returned nothing for {query!r}. "
                "Do not retry this or other searches for now — continue with what you "
                "already have, or say you could not verify this."
            )

        pause = backoff + random.uniform(0, backoff / 2)
        await asyncio.sleep(pause)
        waited += pause
        backoff *= 2


def _format_results(query: str, results: list[tuple[str, str, str]]) -> str:
    lines = [f"{len(results)} result(s) for {query!r}:"]
    for i, (title, url, snippet) in enumerate(results, 1):
        lines.append(f"{i}. {title}\n   {url}\n   {snippet}" if snippet else f"{i}. {title}\n   {url}")
    return "\n".join(lines)


async def _fetch_handler(args: dict[str, Any], dry_run: bool) -> str:
    url = str(args.get("url", "")).strip()
    if not url:
        return "Error: url is required."
    if not url.startswith(("http://", "https://")):
        return "Error: url must start with http:// or https://."

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
            async with client.stream("GET", url, headers=HEADERS) as resp:
                resp.raise_for_status()
                ctype = resp.headers.get("content-type", "").lower()
                if any(t in ctype for t in _BINARY_TYPES):
                    return (
                        f"Error: {url} returned {ctype}, which is not readable as text."
                    )
                chunks, size = [], 0
                async for chunk in resp.aiter_bytes():
                    chunks.append(chunk)
                    size += len(chunk)
                    if size >= MAX_BYTES:
                        break
                encoding = resp.encoding or "utf-8"
                final_url = str(resp.url)
    except httpx.HTTPError as exc:
        return f"Error: could not fetch the page ({type(exc).__name__})."

    payload = b"".join(chunks)
    if _looks_binary(payload[:8192]):
        return f"Error: {url} returned binary data, which is not readable as text."

    raw = payload.decode(encoding, errors="replace")
    # Only markup needs stripping. CSV and JSON are already text, and running them through
    # the tag remover would mangle quoted angle brackets in field values.
    # Markup needs stripping; CSV and JSON do not, and running them through the tag remover
    # would eat any field value containing angle brackets. Sniffed rather than trusted to
    # the header, since a page served as octet-stream is still a page.
    is_markup = "html" in ctype or raw.lstrip()[:200].lower().startswith(("<!doctype html", "<html"))
    text = _to_text(raw, base_url=final_url) if is_markup else raw.strip()

    if not text:
        return f"{url} returned no readable text."
    return _truncate(text)


def _truncate(text: str) -> str:
    """Cap page text at PAGE_CHARS, cutting at a line boundary and saying what was cut.

    A cut mid-line can chop a URL in half — an agent told to only send verified links then
    drops that row entirely. And a silent cut lets the model assume it saw the whole page;
    telling it "38 of 120 lines" is what prompts it to narrow the query or paginate.
    """
    if len(text) <= PAGE_CHARS:
        return text
    total = text.count("\n") + 1
    kept = text[:PAGE_CHARS]
    cut = kept.rfind("\n")
    # A single enormous line (minified JSON, one-line CSV) has no boundary to respect.
    if cut > 0:
        kept = kept[:cut]
    shown = kept.count("\n") + 1
    return (
        f"{kept}\n\n[truncated — showing {shown} of {total} lines. The page continues; "
        "narrow the request (filters, pagination, a more specific URL) to see the rest.]"
    )


# ── Parsing ────────────────────────────────────────────────────────────────────

def _looks_binary(sample: bytes) -> bool:
    """Whether a leading sample is binary rather than text.

    Needed because download endpoints label everything application/octet-stream, so the
    content type cannot decide it. Bytes at or above 128 are left uncounted — those are
    ordinary UTF-8 continuation bytes, not evidence of binary.
    """
    if b"\x00" in sample:
        return True
    printable = bytes(range(32, 127)) + b"\n\r\t\f\b"
    control = sum(1 for b in sample if b < 128 and b not in printable)
    return control / max(len(sample), 1) > 0.30

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


# Elements that are page chrome, not page content. Removed with their contents. Forms are
# included because they hold search/filter UI; sites that put real content inside a form
# are rare enough to accept losing it in exchange for dropping every filter panel.
_CHROME_TAGS = ("script", "style", "noscript", "svg", "template", "iframe",
                "nav", "header", "footer", "aside", "form")
_CHROME_ROLES = '//*[@role="navigation" or @role="banner" or @role="contentinfo" or @role="search"]'


def _to_text(markup: str, base_url: str = "") -> str:
    """HTML to text: drop page chrome from the DOM, keep everything else.

    Anchors keep their destination as "label (absolute url)". A listing page whose rows
    link to detail pages is otherwise unusable — the agent can read every title and reach
    none of them, which is what pushes it into guessing URLs.

    Chrome removal cuts the boilerplate that used to consume most of the page budget
    (a CanadaBuys listing spends ~60% of its text on Canada.ca menus). If lxml cannot
    parse the page at all, the old regex stripper still produces something.
    """
    try:
        return _dom_to_text(markup, base_url)
    except (etree.ParserError, etree.XMLSyntaxError, ValueError):
        return _regex_to_text(markup, base_url)


def _dom_to_text(markup: str, base_url: str) -> str:
    tree = lxml_html.fromstring(markup)
    if base_url:
        try:
            tree.make_links_absolute(base_url)
        except ValueError:
            pass  # malformed base URL — keep links as they are

    for tag in _CHROME_TAGS:
        for el in tree.findall(f".//{tag}"):
            parent = el.getparent()
            if parent is not None:
                parent.remove(el)
    for el in tree.xpath(_CHROME_ROLES):
        parent = el.getparent()
        if parent is not None:
            parent.remove(el)

    for a in tree.findall(".//a"):
        label = " ".join(a.text_content().split())
        href = (a.get("href") or "").strip()
        if label and href and not href.startswith(("#", "javascript:", "data:")):
            a.text = f"{label} ({href})"
            for child in list(a):
                a.remove(child)

    lines = [" ".join(line.split()) for line in tree.text_content().splitlines()]
    return _BLANK_LINES.sub("\n\n", "\n".join(line for line in lines if line)).strip()


def _regex_to_text(markup: str, base_url: str) -> str:
    body = _DROP_BLOCKS.sub(" ", markup)
    body = _ANCHOR.sub(lambda m: _render_anchor(m, base_url), body)
    body = re.sub(r"<(br|/p|/div|/li|/h[1-6])\s*/?>", "\n", body, flags=re.IGNORECASE)
    body = _TAG.sub("", body)
    body = html.unescape(body)
    lines = [" ".join(line.split()) for line in body.splitlines()]
    return _BLANK_LINES.sub("\n\n", "\n".join(line for line in lines if line)).strip()


def _render_anchor(match: re.Match[str], base_url: str) -> str:
    """Flatten one anchor to "label (url)", dropping destinations worth no tokens."""
    label = _clean(match.group("label"))
    if not label:
        return " "  # icon-only or empty link: the href leads nowhere useful without it
    href = html.unescape(match.group("href")).strip()
    if not href or href.startswith(("#", "javascript:", "data:")):
        return label
    if base_url:
        href = urljoin(base_url, href)
    return f"{label} ({href})"

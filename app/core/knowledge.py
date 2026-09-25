"""
Knowledge
=========
Documents an agent can search at run time. The pipeline is deliberately split across the
request/worker boundary by cost:

- **Upload** (API request): parse the file to text and store it. Parsing is fast and a
  corrupt PDF should fail in the uploader's face, not silently in a background job.
- **Index** (worker): chunk the text, embed the chunks through the workspace's own OpenAI
  key, insert them. Embedding needs the network and can take seconds, which is exactly
  what the worker exists for.

Embeddings are billed to the workspace's OpenAI account like every other token the platform
spends — the platform itself holds no key. That makes an OpenAI connector a prerequisite for
Knowledge, which the API and UI both state plainly rather than hiding.

The embedding model is a platform-wide constant (`EMBEDDING_MODEL`): vectors from different
models are not comparable, so a per-workspace choice would fracture every index it touched.
"""

import io

import openai
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.crypto import decrypt_json
from app.db.models import (
    AgentKnowledgeChunk,
    AgentKnowledgeFile,
    Connector,
    ConnectorStatus,
    ConnectorType,
    KnowledgeFileStatus,
)

EMBEDDING_MODEL = "text-embedding-3-small"

# ~1200 chars ≈ 300 tokens: big enough that a chunk carries a complete thought, small
# enough that five of them fit in a tool result without drowning the run's context.
CHUNK_CHARS = 1200
CHUNK_OVERLAP = 200

# OpenAI accepts up to 2048 inputs per call; smaller batches keep request bodies sane.
EMBED_BATCH = 128

# .xlsx is parsed by `tabular` (openpyxl); it has no text path of its own here.
ACCEPTED_EXTENSIONS = (".pdf", ".txt", ".md", ".csv", ".xlsx")

# Caps. A price list is kilobytes; anything near these limits is probably the wrong tool.
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_TEXT_CHARS = 2_000_000


class KnowledgeError(RuntimeError):
    """Surfaced to the uploader (HTTP 422) or stored on the file row (worker)."""


# ── Parsing ────────────────────────────────────────────────────────────────────

def extract_text(filename: str, data: bytes) -> str:
    """File bytes to plain text. Raises KnowledgeError with a user-readable message."""
    lower = filename.lower()

    if lower.endswith(".pdf"):
        try:
            from pypdf import PdfReader

            reader = PdfReader(io.BytesIO(data))
            text = "\n\n".join(page.extract_text() or "" for page in reader.pages)
        except Exception as exc:  # noqa: BLE001 — pypdf raises a zoo of types
            raise KnowledgeError(f"Could not read this PDF: {exc}") from exc
        if not text.strip():
            raise KnowledgeError(
                "This PDF contains no extractable text. Scanned documents are images — "
                "they would need OCR, which is not supported."
            )
    elif lower.endswith(".xlsx"):
        raise KnowledgeError("Workbooks are parsed by the tabular pipeline.")  # see router
    elif lower.endswith(ACCEPTED_EXTENSIONS):
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            text = data.decode("latin-1", errors="replace")
    else:
        raise KnowledgeError(
            f"Unsupported file type. Accepted: {', '.join(ACCEPTED_EXTENSIONS)}"
        )

    if len(text) > MAX_TEXT_CHARS:
        raise KnowledgeError("This document is too large to index.")
    return text.strip()


# ── Chunking ───────────────────────────────────────────────────────────────────

def chunk_text(text: str) -> list[str]:
    """Fixed-size windows with overlap, preferring to break at a paragraph or sentence.

    Overlap exists because answers straddle chunk boundaries: without it, "the refund
    window is" and "30 days" can end up in different chunks and neither retrieves well.
    """
    if not text:
        return []

    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + CHUNK_CHARS, len(text))
        window = text[start:end]

        # Prefer a natural boundary in the back half of the window.
        if end < len(text):
            for sep in ("\n\n", "\n", ". "):
                cut = window.rfind(sep)
                if cut > CHUNK_CHARS // 2:
                    end = start + cut + len(sep)
                    window = text[start:end]
                    break

        cleaned = window.strip()
        if cleaned:
            chunks.append(cleaned)
        if end >= len(text):
            break
        start = end - CHUNK_OVERLAP
    return chunks


# ── Embedding ──────────────────────────────────────────────────────────────────

async def openai_key_for_org(db: AsyncSession, org_id) -> str:
    """The workspace's OpenAI key. Knowledge cannot work without one."""
    row = await db.exec(
        select(Connector).where(
            Connector.org_id == org_id,
            Connector.type == ConnectorType.openai,
            Connector.status == ConnectorStatus.active,
        )
    )
    connector = row.first()
    if connector is None:
        raise KnowledgeError(
            "Knowledge needs an OpenAI connector — it is used to index and search "
            "documents. Connect one on the Connectors page."
        )
    return decrypt_json(connector.config)["api_key"]


async def embed_texts(texts: list[str], api_key: str) -> list[list[float]]:
    client = openai.AsyncOpenAI(api_key=api_key)
    out: list[list[float]] = []
    for i in range(0, len(texts), EMBED_BATCH):
        batch = texts[i : i + EMBED_BATCH]
        try:
            resp = await client.embeddings.create(model=EMBEDDING_MODEL, input=batch)
        except openai.APIError as exc:
            raise KnowledgeError(f"OpenAI embeddings failed: {exc}") from exc
        out.extend(item.embedding for item in resp.data)
    return out


# ── Indexing (worker) ──────────────────────────────────────────────────────────

async def claim_pending_files(db: AsyncSession, *, limit: int) -> list:
    """Claim uploaded-but-unindexed files. SKIP LOCKED so parallel workers divide the work."""
    rows = await db.exec(
        select(AgentKnowledgeFile)
        .where(AgentKnowledgeFile.status == KnowledgeFileStatus.pending)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    files = rows.all()
    for file in files:
        file.status = KnowledgeFileStatus.processing
        db.add(file)
    await db.commit()
    return [file.id for file in files]


async def index_file(db: AsyncSession, file: AgentKnowledgeFile) -> None:
    """Chunk and embed one uploaded file. Sets status to ready or error; never raises."""
    file.status = KnowledgeFileStatus.processing
    db.add(file)
    await db.commit()

    try:
        api_key = await openai_key_for_org(db, file.org_id)
        chunks = chunk_text(file.text)
        if not chunks:
            raise KnowledgeError("The document contains no text to index.")
        vectors = await embed_texts(chunks, api_key)

        for seq, (content, vector) in enumerate(zip(chunks, vectors)):
            db.add(
                AgentKnowledgeChunk(
                    file_id=file.id,
                    agent_id=file.agent_id,
                    seq=seq,
                    content=content,
                    embedding=vector,
                )
            )
        file.status = KnowledgeFileStatus.ready
        file.chunk_count = len(chunks)
        file.error = None
    except KnowledgeError as exc:
        file.status = KnowledgeFileStatus.error
        file.error = str(exc)
    except Exception as exc:  # noqa: BLE001 — a crashed job must not wedge the file in 'processing'
        file.status = KnowledgeFileStatus.error
        file.error = f"{type(exc).__name__}: {exc}"

    db.add(file)
    await db.commit()


# ── Search (run time) ──────────────────────────────────────────────────────────

async def search(db: AsyncSession, agent_id, query: str, *, limit: int = 5) -> list[dict]:
    """The `search_knowledge` tool's backend: embed the query, nearest chunks by cosine.

    Raises KnowledgeError when the workspace has no OpenAI key — the tool layer turns
    that into a readable tool result rather than a crashed run.
    """
    file_row = await db.exec(
        select(AgentKnowledgeFile.org_id).where(AgentKnowledgeFile.agent_id == agent_id).limit(1)
    )
    org_id = file_row.first()
    if org_id is None:
        return []

    api_key = await openai_key_for_org(db, org_id)
    vector = (await embed_texts([query], api_key))[0]

    rows = await db.exec(
        select(AgentKnowledgeChunk, AgentKnowledgeFile.filename)
        .join(AgentKnowledgeFile, AgentKnowledgeChunk.file_id == AgentKnowledgeFile.id)
        .where(
            AgentKnowledgeChunk.agent_id == agent_id,
            AgentKnowledgeFile.status == KnowledgeFileStatus.ready,
        )
        .order_by(AgentKnowledgeChunk.embedding.cosine_distance(vector))
        .limit(limit)
    )
    return [
        {"filename": filename, "content": chunk.content}
        for chunk, filename in rows.all()
    ]


async def build_tool(db: AsyncSession, agent_id):
    """The `search_knowledge` tool for one run, or None when the agent has no indexed files.

    Absent rather than present-but-empty: a model offered a tool will try it, and an agent
    with no documents should not spend an iteration learning that.
    """
    # Imported here, mirroring websearch: the runtime imports this module lazily, and
    # importing base at module load would only tighten the knot for no benefit.
    from app.core.agents.base import RegisteredTool
    from app.core.llm.client import ToolSpec

    ready = await db.exec(
        select(AgentKnowledgeFile.id).where(
            AgentKnowledgeFile.agent_id == agent_id,
            AgentKnowledgeFile.status == KnowledgeFileStatus.ready,
        ).limit(1)
    )
    if ready.first() is None:
        return None

    async def handler(args: dict, dry_run: bool) -> str:
        # Read-only, so dry_run needs no special casing.
        query = str(args.get("query", "")).strip()
        if not query:
            return "Provide a query describing what to look for."
        try:
            results = await search(db, agent_id, query)
        except KnowledgeError as exc:
            return f"Knowledge search is unavailable: {exc}"
        if not results:
            return "No matching passages in the knowledge files."
        return "\n\n".join(f"[{r['filename']}]\n{r['content']}" for r in results)

    return RegisteredTool(
        spec=ToolSpec(
            name="search_knowledge",
            description=(
                "Search the documents uploaded to this agent (price lists, policies, "
                "FAQs). Use this whenever the answer might be in those files."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "What to look for, phrased as a question or topic.",
                    }
                },
                "required": ["query"],
            },
        ),
        handler=handler,
    )


# ── URL ingestion ──────────────────────────────────────────────────────────────

MAX_URL_BYTES = 5 * 1024 * 1024  # 5 MB — web pages are rarely bigger


async def fetch_url_text(url: str) -> tuple[str, str]:
    """Fetch a URL and return (title_or_url, plain_text).

    Uses html2text to strip markup; falls back to raw response body for
    plain-text content types (txt, md, csv).

    Raises KnowledgeError with a user-readable message on failure.
    """
    import html2text
    import httpx

    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
            resp = await client.get(url, headers={"User-Agent": "setod-knowledge/1.0"})
            resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise KnowledgeError(
            f"The server returned {exc.response.status_code} for that URL."
        ) from exc
    except httpx.RequestError as exc:
        raise KnowledgeError(f"Could not reach that URL: {exc}") from exc

    raw = resp.content
    if len(raw) > MAX_URL_BYTES:
        raise KnowledgeError(
            f"Page is too large ({len(raw) // 1024} KB). Limit is {MAX_URL_BYTES // 1024} KB."
        )

    content_type = resp.headers.get("content-type", "")
    if "html" in content_type:
        h = html2text.HTML2Text()
        h.ignore_links = False
        h.ignore_images = True
        h.body_width = 0
        text = h.handle(raw.decode("utf-8", errors="replace"))
    else:
        text = raw.decode("utf-8", errors="replace")

    text = text.strip()
    if not text:
        raise KnowledgeError("The page contained no extractable text.")

    # Use the final URL (after redirects) as the display name.
    display = str(resp.url)
    return display, text

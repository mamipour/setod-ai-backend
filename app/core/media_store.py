"""
Local media store
=================
Bytes for conversation attachments are kept on disk under `settings.media_dir` (default
`/var/lib/setod/media`) with the path layout::

    {org_id}/{conv_id}/{msg_id}-{idx}.{ext}

Hard cap: 20 MB per file.  Anything larger is rejected — the caller should fall back
to storing a marker instead.

The store is intentionally simple.  A future version may push to S3 if the disk fills up,
but for the current scale (single-host), local disk is the right trade-off.
"""
from __future__ import annotations

import hashlib
import logging
import mimetypes
import os
from pathlib import Path
from uuid import UUID

log = logging.getLogger(__name__)

MAX_BYTES = 20 * 1024 * 1024  # 20 MB hard cap


def _media_root() -> Path:
    from app.config import settings
    return Path(settings.media_dir)


def _ext_for_mime(mime: str, fallback: str = "bin") -> str:
    """Guess a file extension for a MIME type."""
    ext = mimetypes.guess_extension(mime, strict=False)
    if ext:
        return ext.lstrip(".")
    # Common overrides that mimetypes misses
    overrides = {
        "audio/ogg": "ogg",
        "audio/mp4": "m4a",
        "video/mp4": "mp4",
        "image/jpeg": "jpg",
        "image/webp": "webp",
        "application/pdf": "pdf",
    }
    return overrides.get(mime, fallback)


def _path(org_id: UUID, conv_id: UUID, msg_id: UUID, idx: int, ext: str) -> Path:
    return _media_root() / str(org_id) / str(conv_id) / f"{msg_id}-{idx}.{ext}"


def put(
    org_id: UUID,
    conv_id: UUID,
    msg_id: UUID,
    idx: int,
    data: bytes,
    mime: str = "application/octet-stream",
) -> str:
    """Write media bytes to disk. Returns the stored path string, or raises ValueError on oversize."""
    if len(data) > MAX_BYTES:
        raise ValueError(f"Media file too large: {len(data)} bytes (cap {MAX_BYTES})")
    ext = _ext_for_mime(mime)
    dest = _path(org_id, conv_id, msg_id, idx, ext)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    log.debug("media stored: %s (%d bytes)", dest, len(data))
    return str(dest)


def get(stored_path: str) -> bytes:
    """Read bytes from a stored path.  Raises FileNotFoundError if the file is missing."""
    return Path(stored_path).read_bytes()


def delete(stored_path: str) -> bool:
    """Delete a stored file. Returns True if deleted, False if it was already gone."""
    p = Path(stored_path)
    if p.exists():
        p.unlink()
        return True
    return False


def delete_conversation(org_id: UUID, conv_id: UUID) -> int:
    """Delete all media files for a conversation. Returns the count of files deleted."""
    directory = _media_root() / str(org_id) / str(conv_id)
    count = 0
    if directory.is_dir():
        for f in directory.iterdir():
            try:
                f.unlink()
                count += 1
            except Exception:
                log.warning("could not delete media file %s", f)
        try:
            directory.rmdir()
        except Exception:
            pass
    return count

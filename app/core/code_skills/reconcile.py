"""Delete Lambda functions that no longer have a code_skills row.

Only functions tagged ``setod-env`` with this process's APP_ENV are removed, so a
staging run cannot delete production functions.
"""
from __future__ import annotations

import logging

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import settings
from app.core.code_skills import get_backend
from app.core.code_skills.aws import LambdaBackend
from app.db.models import CodeSkill

log = logging.getLogger("setod.code_skills")


async def reconcile_orphans(db: AsyncSession, backend: LambdaBackend | None = None) -> list[str]:
    if not settings.code_skills_enabled:
        return []
    backend = backend or get_backend()
    live = await backend.list_function_names(prefix="setod-cs-")
    known = set((await db.exec(select(CodeSkill.lambda_function_name))).all())
    known.discard(None)
    deleted: list[str] = []
    for name in live:
        if name in known:
            continue
        env_tag = await backend.function_env_tag(function_name=name)
        if env_tag != settings.app_env:
            continue
        await backend.delete(function_name=name)
        deleted.append(name)
        log.info("deleted orphan code-skill function %s", name)
    return deleted

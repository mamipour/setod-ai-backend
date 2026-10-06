"""Validate, package, deploy, and invoke code skills. No boto3 imports here."""
from __future__ import annotations

import ast
import asyncio
import hashlib
import io
import json
import logging
import re
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import settings
from app.core import crypto
from app.core.billing.entitlements import EntitlementError, resolve
from app.core.billing.usage import record_event
from app.core.code_skills.aws import InvokeResult, LambdaBackend
from app.db.models import CodeSkill, CodeSkillDeploy, CodeSkillDeployStatus, User
from app.db.session import AsyncSessionLocal

log = logging.getLogger("setod.code_skills")

MAX_SOURCE_BYTES = 65_536
MAX_INPUT_BYTES = 65_536
MAX_MODEL_CHARS = 16_384
MAX_DEPLOYS_PER_HOUR = 30
_TOOL_NAME = re.compile(r"^[a-z][a-z0-9_]{2,39}$")
_SECRET_KEY = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_RESERVED_EXACT = {"PATH", "PYTHONPATH", "_HANDLER"}
_RESERVED_PREFIXES = ("AWS_", "LAMBDA_", "SETOD_")
_HANDLER_PATH = Path(__file__).with_name("lambda_handler.py")

_tasks: set[asyncio.Task] = set()


class CodeSkillValidationError(ValueError):
    """The user's source, schema, name, or secrets were rejected."""


class CodeSkillRateLimited(RuntimeError):
    """This org has started too many deploys in the last hour."""


def source_sha(source: str) -> str:
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def function_name_for(skill_id: UUID) -> str:
    return "setod-cs-" + skill_id.hex


def validate_source(source: str) -> None:
    if "\x00" in source:
        raise CodeSkillValidationError("Source contains a null byte")
    if len(source.encode("utf-8")) > MAX_SOURCE_BYTES:
        raise CodeSkillValidationError("Source is larger than 65536 bytes")
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise CodeSkillValidationError(f"Syntax error: {exc.msg}") from exc
    has_main = any(
        isinstance(node, ast.FunctionDef) and node.name == "main"
        for node in tree.body
    )
    if not has_main:
        raise CodeSkillValidationError("Source must define a top-level function main(input, context)")


def validate_tool_name(name: str) -> None:
    if not _TOOL_NAME.fullmatch(name or ""):
        raise CodeSkillValidationError(
            "Tool name must be 3–40 characters: a lowercase letter, then lowercase letters, digits, or underscores"
        )


def validate_input_schema(schema: object) -> None:
    if not isinstance(schema, dict):
        raise CodeSkillValidationError("Input schema must be a JSON object")
    if schema.get("type") != "object":
        raise CodeSkillValidationError("Input schema type must be 'object'")
    if not isinstance(schema.get("properties"), dict):
        raise CodeSkillValidationError("Input schema must include a properties object")


def validate_secrets(secrets: dict[str, str]) -> None:
    if len(secrets) > 20:
        raise CodeSkillValidationError("At most 20 secrets are allowed")
    total = 0
    for key, value in secrets.items():
        if not _SECRET_KEY.fullmatch(key):
            raise CodeSkillValidationError(
                f"Secret name {key!r} must be uppercase letters, digits, and underscores, starting with a letter"
            )
        if key in _RESERVED_EXACT or key.startswith(_RESERVED_PREFIXES):
            raise CodeSkillValidationError(f"Secret name {key!r} is reserved")
        if len(value) > 4096:
            raise CodeSkillValidationError(f"Secret {key!r} is longer than 4096 characters")
        total += len(key) + len(value)
    # Lambda's whole environment, including our SETOD_* keys, must stay under 4 KB.
    if total > 3500:
        raise CodeSkillValidationError("Secrets are larger than the Lambda environment limit")


def validate_timeout(timeout_seconds: int) -> None:
    if not isinstance(timeout_seconds, int) or isinstance(timeout_seconds, bool) or not 1 <= timeout_seconds <= 30:
        raise CodeSkillValidationError("Timeout must be an integer from 1 to 30 seconds")


def validate_text_fields(*, name: str, tagline: str, tool_description: str) -> None:
    if not name or len(name) > 80:
        raise CodeSkillValidationError("Name must be 1–80 characters")
    if len(tagline) > 160:
        raise CodeSkillValidationError("Tagline must be at most 160 characters")
    if not tool_description or len(tool_description) > 1000:
        raise CodeSkillValidationError("Tool description must be 1–1000 characters")


def build_zip(source: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("handler.py", _HANDLER_PATH.read_bytes())
        zf.writestr("user_code.py", source.encode("utf-8"))
    return buf.getvalue()


def environment_for(skill: CodeSkill, secrets: dict[str, str]) -> dict[str, str]:
    env = {
        "SETOD_SKILL_ID": str(skill.id),
        "SETOD_ORG_ID": str(skill.org_id),
    }
    env.update(secrets)
    return env


def decrypt_secrets(skill: CodeSkill) -> dict[str, str]:
    if not skill.secrets_enc:
        return {}
    raw = crypto.decrypt_json(skill.secrets_enc)
    return {str(k): str(v) for k, v in raw.items()}


def result_text(parsed: dict | None) -> str:
    """The string handed back to the model. Tracebacks stay out of it."""
    if not parsed or not parsed.get("ok"):
        err = (parsed or {}).get("error") or "function returned a response that was not JSON"
        return f"Error: {err}"
    result = parsed.get("result")
    if parsed.get("truncated") and isinstance(result, str):
        text = result
    else:
        text = json.dumps(result, ensure_ascii=False, default=str)
    if len(text) > MAX_MODEL_CHARS:
        return text[:MAX_MODEL_CHARS] + " …[truncated]"
    return text


def _spawn(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return task


async def deploys_in_last_hour(db: AsyncSession, org_id: UUID) -> int:
    cutoff = datetime.now(UTC) - timedelta(hours=1)
    rows = (await db.exec(
        select(CodeSkillDeploy.id).where(
            CodeSkillDeploy.org_id == org_id,
            CodeSkillDeploy.started_at >= cutoff,
        )
    )).all()
    return len(rows)


async def start_deploy(db: AsyncSession, skill: CodeSkill, user: User, backend: LambdaBackend) -> CodeSkillDeploy:
    if await deploys_in_last_hour(db, skill.org_id) >= MAX_DEPLOYS_PER_HOUR:
        raise CodeSkillRateLimited("This workspace has started 30 deploys in the last hour. Try again later.")
    skill.deploy_status = CodeSkillDeployStatus.deploying.value
    skill.last_deploy_error = None
    skill.updated_at = datetime.now(UTC)
    deploy = CodeSkillDeploy(
        code_skill_id=skill.id,
        org_id=skill.org_id,
        requested_by_id=user.id,
        source_sha256=skill.source_sha256,
        network_access=skill.network_access,
        outcome="pending",
    )
    db.add(skill)
    db.add(deploy)
    await db.commit()
    await db.refresh(deploy)
    _spawn(_run_deploy(skill.id, deploy.id, backend))
    return deploy


async def _run_deploy(skill_id: UUID, deploy_id: UUID, backend: LambdaBackend) -> None:
    async with AsyncSessionLocal() as db:
        skill = await db.get(CodeSkill, skill_id)
        deploy = await db.get(CodeSkillDeploy, deploy_id)
        if skill is None or deploy is None:
            return
        try:
            secrets = decrypt_secrets(skill)
            result = await backend.deploy(
                function_name=function_name_for(skill.id),
                zip_bytes=build_zip(skill.source),
                timeout_seconds=skill.timeout_seconds,
                network_access=skill.network_access,
                env=environment_for(skill, secrets),
                tags={
                    "setod-org": str(skill.org_id),
                    "setod-skill": str(skill.id),
                    "setod-env": settings.app_env,
                },
            )
        except Exception as exc:  # noqa: BLE001 — recorded on the row, never raised
            log.exception("code skill deploy failed skill=%s", skill_id)
            message = f"{type(exc).__name__}: {exc}"[:2000]
            skill.deploy_status = CodeSkillDeployStatus.failed.value
            skill.last_deploy_error = message
            deploy.outcome = "failed"
            deploy.error = message
        else:
            skill.deploy_status = CodeSkillDeployStatus.ready.value
            skill.deployed_sha256 = skill.source_sha256
            skill.deployed_network_access = skill.network_access
            skill.deployed_timeout_seconds = skill.timeout_seconds
            skill.lambda_function_name = result["function_name"]
            skill.lambda_arn = result["function_arn"]
            skill.last_deploy_error = None
            skill.last_deployed_at = datetime.now(UTC)
            deploy.outcome = "succeeded"
            deploy.error = None
        deploy.finished_at = datetime.now(UTC)
        skill.updated_at = datetime.now(UTC)
        db.add(skill)
        db.add(deploy)
        await db.commit()


async def invoke(
    db: AsyncSession,
    skill: CodeSkill,
    *,
    args: dict,
    ctx: dict,
    backend: LambdaBackend,
    want_logs: bool = False,
    idempotency_key: str,
    agent_id: UUID | None,
    session_id: UUID | None,
) -> tuple[str, InvokeResult | None]:
    ent = await resolve(db, skill.org_id)
    try:
        ent.require_quota("code_invocations")
    except EntitlementError:
        return "Error: code skill quota reached for this workspace.", None

    encoded = json.dumps(args, default=str)
    if len(encoded.encode("utf-8")) > MAX_INPUT_BYTES:
        return "Error: input too large", None

    payload = {"input": args, "context": ctx}
    try:
        res = await backend.invoke(
            function_name=skill.lambda_function_name or function_name_for(skill.id),
            payload=payload,
            timeout_seconds=skill.timeout_seconds,
            want_logs=want_logs,
        )
    except Exception as exc:  # noqa: BLE001
        text = f"Error: {type(exc).__name__}: {exc}"
        skill.last_error = text[:2000]
        skill.invocation_count += 1
        skill.last_invoked_at = datetime.now(UTC)
        db.add(skill)
        await db.commit()
        return text, None

    text = result_text(res["payload"])
    ok = bool(res["payload"] and res["payload"].get("ok"))
    if not ok:
        skill.last_error = text[:2000]
    skill.invocation_count += 1
    skill.last_invoked_at = datetime.now(UTC)
    db.add(skill)
    await record_event(
        db,
        org_id=skill.org_id,
        meter="code_invocations",
        quantity=1.0,
        idempotency_key=idempotency_key,
        billable=True,
        agent_id=agent_id,
        session_id=session_id,
        cost_usd=0.0,
        meta={
            "skill_id": str(skill.id),
            "duration_ms": res["duration_ms"],
            "ok": ok,
        },
    )
    return text, res


async def delete_skill(db: AsyncSession, skill: CodeSkill, backend: LambdaBackend | None) -> None:
    if skill.lambda_function_name and backend is not None:
        await backend.delete(function_name=skill.lambda_function_name)
    await db.delete(skill)
    await db.commit()


async def sweep_interrupted_deploys() -> int:
    """Mark deploys still 'deploying' after a process restart as failed."""
    cutoff = datetime.now(UTC) - timedelta(minutes=10)
    async with AsyncSessionLocal() as db:
        rows = (await db.exec(
            select(CodeSkill).where(CodeSkill.deploy_status == CodeSkillDeployStatus.deploying.value)
        )).all()
        n = 0
        for skill in rows:
            if skill.updated_at and skill.updated_at >= cutoff:
                continue
            skill.deploy_status = CodeSkillDeployStatus.failed.value
            skill.last_deploy_error = "Interrupted by restart"
            skill.updated_at = datetime.now(UTC)
            db.add(skill)
            n += 1
        if n:
            await db.commit()
        return n

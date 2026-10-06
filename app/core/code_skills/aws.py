"""AWS Lambda calls for code skills. All boto3 work happens in a thread."""
from __future__ import annotations

import asyncio
import base64
import json
import logging
from datetime import UTC, datetime
from typing import Protocol, TypedDict

from botocore.config import Config
from botocore.exceptions import ClientError, ReadTimeoutError

from app.config import settings

log = logging.getLogger("setod.code_skills")


class DeployResult(TypedDict):
    function_name: str
    function_arn: str


class InvokeResult(TypedDict):
    status_code: int
    function_error: str | None
    payload: dict | None
    log_tail: str | None
    duration_ms: int


class CodeSkillInvokeError(RuntimeError):
    """The invoke call itself failed (network, auth), before user code ran."""


class LambdaBackend(Protocol):
    async def deploy(
        self,
        *,
        function_name: str,
        zip_bytes: bytes,
        timeout_seconds: int,
        network_access: bool,
        env: dict[str, str],
        tags: dict[str, str],
    ) -> DeployResult: ...

    async def invoke(self, *, function_name: str, payload: dict, timeout_seconds: int, want_logs: bool = False) -> InvokeResult: ...

    async def delete(self, *, function_name: str) -> None: ...

    async def list_function_names(self, *, prefix: str = "setod-cs-") -> list[str]: ...

    async def function_env_tag(self, *, function_name: str) -> str | None: ...


def vpc_config(*, network_access: bool) -> dict:
    """VpcConfig for create/update.

    network_access False attaches the no-egress VPC. True passes empty lists, which is
    how UpdateFunctionConfiguration removes a VPC config. CreateFunction omits the key
    entirely when network is on; callers check ``network_access`` for that.
    """
    if network_access:
        return {"SubnetIds": [], "SecurityGroupIds": []}
    subnets = [s.strip() for s in settings.aws_usercode_subnet_ids.split(",") if s.strip()]
    return {
        "SubnetIds": subnets,
        "SecurityGroupIds": [settings.aws_usercode_security_group_id],
    }


class BotoLambdaBackend:
    """Assumes the deployer role, then creates, updates, invokes, and deletes functions."""

    def __init__(self) -> None:
        self._creds: dict | None = None
        self._expiration: datetime | None = None
        self._lock = asyncio.Lock()

    async def _credentials(self) -> dict:
        async with self._lock:
            now = datetime.now(UTC)
            if self._creds and self._expiration and (self._expiration - now).total_seconds() > 300:
                return self._creds

            def _assume() -> dict:
                import boto3
                sts = boto3.client(
                    "sts",
                    region_name=settings.aws_region,
                    aws_access_key_id=settings.aws_access_key_id or None,
                    aws_secret_access_key=settings.aws_secret_access_key or None,
                )
                resp = sts.assume_role(
                    RoleArn=settings.aws_usercode_deployer_role_arn,
                    RoleSessionName="setod-platform",
                    ExternalId=settings.aws_usercode_external_id,
                    DurationSeconds=3600,
                )
                return resp["Credentials"]

            creds = await asyncio.to_thread(_assume)
            exp = creds["Expiration"]
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=UTC)
            self._creds = creds
            self._expiration = exp
            return creds

    def _client(self, creds: dict, *, read_timeout: int, max_attempts: int):
        import boto3
        # max_attempts is the total number of calls. 1 means no retry: a retried
        # side-effecting invoke would run user code twice.
        return boto3.client(
            "lambda",
            region_name=settings.aws_region,
            aws_access_key_id=creds["AccessKeyId"],
            aws_secret_access_key=creds["SecretAccessKey"],
            aws_session_token=creds["SessionToken"],
            config=Config(
                connect_timeout=5,
                read_timeout=read_timeout,
                retries={"max_attempts": max_attempts, "mode": "standard"},
            ),
        )

    async def deploy(
        self,
        *,
        function_name: str,
        zip_bytes: bytes,
        timeout_seconds: int,
        network_access: bool,
        env: dict[str, str],
        tags: dict[str, str],
    ) -> DeployResult:
        creds = await self._credentials()
        client = self._client(creds, read_timeout=70, max_attempts=2)
        vpc = vpc_config(network_access=network_access)

        def _run() -> str:
            exists = True
            try:
                client.get_function(FunctionName=function_name)
            except client.exceptions.ResourceNotFoundException:
                exists = False

            if not exists:
                kwargs = dict(
                    FunctionName=function_name,
                    Runtime="python3.12",
                    Role=settings.aws_usercode_exec_role_arn,
                    Handler="handler.lambda_handler",
                    Code={"ZipFile": zip_bytes},
                    Timeout=timeout_seconds,
                    MemorySize=256,
                    Architectures=["arm64"],
                    Environment={"Variables": env},
                    Tags=tags,
                    Publish=False,
                )
                if not network_access:
                    kwargs["VpcConfig"] = vpc
                resp = client.create_function(**kwargs)
                arn = resp["FunctionArn"]
                client.get_waiter("function_active_v2").wait(
                    FunctionName=function_name,
                    WaiterConfig={"Delay": 2, "MaxAttempts": 60},
                )
            else:
                client.update_function_code(
                    FunctionName=function_name,
                    ZipFile=zip_bytes,
                    Architectures=["arm64"],
                )
                client.get_waiter("function_updated_v2").wait(
                    FunctionName=function_name,
                    WaiterConfig={"Delay": 2, "MaxAttempts": 60},
                )
                conf = dict(
                    FunctionName=function_name,
                    Timeout=timeout_seconds,
                    Environment={"Variables": env},
                    VpcConfig=vpc,
                )
                resp = client.update_function_configuration(**conf)
                arn = resp["FunctionArn"]
                client.get_waiter("function_updated_v2").wait(
                    FunctionName=function_name,
                    WaiterConfig={"Delay": 2, "MaxAttempts": 60},
                )
            client.put_function_concurrency(
                FunctionName=function_name,
                ReservedConcurrentExecutions=2,
            )
            return arn

        arn = await asyncio.to_thread(_run)
        return {"function_name": function_name, "function_arn": arn}

    async def invoke(
        self,
        *,
        function_name: str,
        payload: dict,
        timeout_seconds: int,
        want_logs: bool = False,
    ) -> InvokeResult:
        creds = await self._credentials()
        client = self._client(creds, read_timeout=timeout_seconds + 5, max_attempts=1)
        started = asyncio.get_event_loop().time()

        def _run() -> dict:
            try:
                resp = client.invoke(
                    FunctionName=function_name,
                    InvocationType="RequestResponse",
                    LogType="Tail" if want_logs else "None",
                    Payload=json.dumps(payload).encode(),
                )
            except (ReadTimeoutError, ClientError) as exc:
                raise CodeSkillInvokeError(str(exc)) from exc
            raw = resp["Payload"].read()
            try:
                parsed = json.loads(raw) if raw else None
            except json.JSONDecodeError:
                parsed = None
            log_tail = None
            if resp.get("LogResult"):
                log_tail = base64.b64decode(resp["LogResult"]).decode(errors="replace")
            function_error = resp.get("FunctionError")
            if function_error:
                message = ""
                if isinstance(parsed, dict):
                    message = str(parsed.get("errorMessage") or "")
                if "Task timed out" in message or (isinstance(parsed, dict) and parsed.get("errorType") == "Sandbox.Timedout"):
                    parsed = {"ok": False, "error": f"Function timed out after {timeout_seconds} s"}
                else:
                    parsed = {"ok": False, "error": message or "Function failed"}
            return {
                "status_code": int(resp.get("StatusCode") or 0),
                "function_error": function_error,
                "payload": parsed if isinstance(parsed, dict) else None,
                "log_tail": log_tail,
            }

        try:
            result = await asyncio.to_thread(_run)
        except CodeSkillInvokeError:
            raise
        result["duration_ms"] = int((asyncio.get_event_loop().time() - started) * 1000)
        return result

    async def delete(self, *, function_name: str) -> None:
        creds = await self._credentials()
        client = self._client(creds, read_timeout=30, max_attempts=2)

        def _run() -> None:
            try:
                client.delete_function(FunctionName=function_name)
            except client.exceptions.ResourceNotFoundException:
                return

        await asyncio.to_thread(_run)

    async def list_function_names(self, *, prefix: str = "setod-cs-") -> list[str]:
        creds = await self._credentials()
        client = self._client(creds, read_timeout=30, max_attempts=2)

        def _run() -> list[str]:
            names: list[str] = []
            paginator = client.get_paginator("list_functions")
            for page in paginator.paginate():
                for fn in page.get("Functions", []):
                    name = fn.get("FunctionName") or ""
                    if name.startswith(prefix):
                        names.append(name)
            return names

        return await asyncio.to_thread(_run)

    async def function_env_tag(self, *, function_name: str) -> str | None:
        creds = await self._credentials()
        client = self._client(creds, read_timeout=30, max_attempts=2)

        def _run() -> str | None:
            resp = client.list_tags(Resource=_function_arn(client, function_name))
            tags = resp.get("Tags") or {}
            return tags.get("setod-env")

        return await asyncio.to_thread(_run)


def _function_arn(client, function_name: str) -> str:
    resp = client.get_function(FunctionName=function_name)
    return resp["Configuration"]["FunctionArn"]

"""Code skills: validation, packaging, the Lambda wrapper, and invoke mapping.

No AWS and no database. The fake backend runs the real handler.py from the zip.
"""
from uuid import uuid4

import pytest

from app.core.billing.entitlements import Entitlements
from app.core.code_skills import service
from app.core.code_skills.service import (
    CodeSkillRateLimited,
    CodeSkillValidationError,
    build_zip,
    environment_for,
    result_text,
    source_sha,
    validate_input_schema,
    validate_secrets,
    validate_source,
    validate_tool_name,
)
from app.db.models import CodeSkill
from tests.fakes.lambda_backend import FakeLambdaBackend

CONTRACT = (
    "def main(input, context):\n"
    "    return {'echo': input}\n"
)


def test_validate_source_accepts_contract():
    validate_source(CONTRACT)


@pytest.mark.parametrize("source", [
    "def main(:\n",
    "def other():\n    return 1\n",
    "class Box:\n    def main(self, input, context):\n        return 1\n",
    "x = 1\n" + ("#" * 70_000),
    "def main(input, context):\n    return '\x00'\n",
])
def test_validate_source_rejects(source):
    with pytest.raises(CodeSkillValidationError):
        validate_source(source)


def test_validate_tool_name():
    validate_tool_name("my_tool")
    for bad in ("My-Tool", "1abc", "ab", "a" * 41):
        with pytest.raises(CodeSkillValidationError):
            validate_tool_name(bad)


def test_validate_input_schema():
    validate_input_schema({"type": "object", "properties": {}})
    for bad in ({"type": "string"}, [], {"type": "object"}):
        with pytest.raises(CodeSkillValidationError):
            validate_input_schema(bad)


def test_build_zip_contains_wrapper_bytes():
    from pathlib import Path
    import io
    import zipfile
    blob = build_zip(CONTRACT)
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        assert sorted(zf.namelist()) == ["handler.py", "user_code.py"]
        handler = zf.read("handler.py")
        expected = Path("app/core/code_skills/lambda_handler.py").read_bytes()
    assert handler == expected


def test_source_sha_is_stable():
    assert source_sha("abc") == source_sha("abc")
    assert source_sha("abc") != source_sha("abd")


@pytest.mark.asyncio
async def test_wrapper_success_error_and_truncation():
    backend = FakeLambdaBackend()
    name = "setod-cs-" + uuid4().hex

    await backend.deploy(
        function_name=name, zip_bytes=build_zip(CONTRACT), timeout_seconds=10,
        network_access=False, env={}, tags={"setod-env": "development"},
    )
    ok = await backend.invoke(function_name=name, payload={"input": {"a": 1}, "context": {}}, timeout_seconds=10)
    assert ok["payload"]["ok"] is True
    assert ok["payload"]["result"] == {"echo": {"a": 1}}
    assert result_text(ok["payload"]) == '{"echo": {"a": 1}}'

    await backend.deploy(
        function_name=name,
        zip_bytes=build_zip("def main(input, context):\n    raise ValueError('x')\n"),
        timeout_seconds=10, network_access=False, env={}, tags={},
    )
    bad = await backend.invoke(function_name=name, payload={"input": {}, "context": {}}, timeout_seconds=10)
    assert bad["payload"]["ok"] is False
    assert bad["payload"]["error"].startswith("ValueError: x")
    assert result_text(bad["payload"]) == "Error: ValueError: x"
    assert "traceback" not in result_text(bad["payload"])

    circular = (
        "def main(input, context):\n"
        "    loop = {}\n"
        "    loop['self'] = loop\n"
        "    return loop\n"
    )
    await backend.deploy(
        function_name=name,
        zip_bytes=build_zip(circular),
        timeout_seconds=10, network_access=False, env={}, tags={},
    )
    ugly = await backend.invoke(function_name=name, payload={"input": {}, "context": {}}, timeout_seconds=10)
    assert ugly["payload"]["ok"] is False
    assert "not JSON-serialisable" in ugly["payload"]["error"]

    huge = "def main(input, context):\n    return 'z' * 70000\n"
    await backend.deploy(
        function_name=name, zip_bytes=build_zip(huge), timeout_seconds=10,
        network_access=False, env={}, tags={},
    )
    truncated = await backend.invoke(function_name=name, payload={"input": {}, "context": {}}, timeout_seconds=10)
    assert truncated["payload"]["truncated"] is True
    text = result_text(truncated["payload"])
    assert len(text) <= 16_384 + len(" …[truncated]")


def test_secrets_reject_reserved_names_and_accept_normal_ones():
    with pytest.raises(CodeSkillValidationError):
        validate_secrets({"AWS_SECRET": "x"})
    with pytest.raises(CodeSkillValidationError):
        validate_secrets({"SETOD_TOKEN": "x"})
    validate_secrets({"API_KEY": "abc"})


def test_environment_includes_secret_and_setod_ids():
    skill = CodeSkill(
        org_id=uuid4(),
        created_by_id=uuid4(),
        name="Add",
        tool_name="add_numbers",
        tool_description="Adds.",
        input_schema={"type": "object", "properties": {}},
        source=CONTRACT,
        source_sha256=source_sha(CONTRACT),
    )
    env = environment_for(skill, {"API_KEY": "abc"})
    assert env["API_KEY"] == "abc"
    assert env["SETOD_SKILL_ID"] == str(skill.id)
    assert env["SETOD_ORG_ID"] == str(skill.org_id)
    assert "abc" not in skill.source


@pytest.mark.asyncio
async def test_deploy_rate_limit():
    class Rows:
        def all(self):
            return list(range(30))

    class DB:
        async def exec(self, _query):
            return Rows()

    skill = type("S", (), {"org_id": uuid4()})()
    with pytest.raises(CodeSkillRateLimited):
        await service.start_deploy(DB(), skill, type("U", (), {"id": uuid4()})(), FakeLambdaBackend())


@pytest.mark.asyncio
async def test_quota_blocks_invoke_before_lambda(monkeypatch):
    async def _resolve(_db, _org_id):
        return Entitlements(plan_code="pro", caps={"code_invocations": 0})

    monkeypatch.setattr(service, "resolve", _resolve)
    backend = FakeLambdaBackend()
    skill = CodeSkill(
        org_id=uuid4(),
        created_by_id=uuid4(),
        name="Add",
        tool_name="add_numbers",
        tool_description="Adds.",
        input_schema={"type": "object", "properties": {}},
        source=CONTRACT,
        source_sha256=source_sha(CONTRACT),
        lambda_function_name="setod-cs-" + uuid4().hex,
    )
    text, res = await service.invoke(
        db=object(),
        skill=skill,
        args={"a": 1},
        ctx={},
        backend=backend,
        idempotency_key="k",
        agent_id=None,
        session_id=None,
    )
    assert text == "Error: code skill quota reached for this workspace."
    assert res is None
    assert backend.invoke_count == 0


@pytest.mark.asyncio
async def test_build_tools_disabled(monkeypatch):
    from app.config import settings
    from app.core.code_skills import tools as code_skill_tools
    monkeypatch.setattr(settings, "code_skills_enabled", False)
    built, approval = await code_skill_tools.build_tools(None, None, None)
    assert built == []
    assert approval == set()


@pytest.mark.asyncio
async def test_fake_deploy_records_network_off():
    backend = FakeLambdaBackend()
    name = "setod-cs-" + uuid4().hex
    await backend.deploy(
        function_name=name, zip_bytes=build_zip(CONTRACT), timeout_seconds=8,
        network_access=False, env={"API_KEY": "abc"}, tags={"setod-env": "development"},
    )
    assert backend.functions[name]["network_access"] is False
    assert backend.functions[name]["env"]["API_KEY"] == "abc"
    backend.fail_next = True
    with pytest.raises(RuntimeError):
        await backend.deploy(
            function_name=name, zip_bytes=b"zip", timeout_seconds=8,
            network_access=True, env={}, tags={},
        )


def test_result_text_unparseable():
    assert result_text(None).startswith("Error:")

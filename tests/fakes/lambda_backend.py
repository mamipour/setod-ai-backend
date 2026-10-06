"""In-process Lambda stand-in. `invoke` runs the real wrapper from the uploaded zip."""
from __future__ import annotations

import importlib
import io
import sys
import tempfile
import time
import zipfile
from pathlib import Path


class FakeLambdaBackend:
    def __init__(self) -> None:
        self.functions: dict[str, dict] = {}
        self.deploy_calls: list[dict] = []
        self.invoke_count = 0
        self.fail_next = False
        self.fail_message = "deploy failed"

    async def deploy(self, *, function_name, zip_bytes, timeout_seconds, network_access, env, tags):
        self.deploy_calls.append({
            "function_name": function_name,
            "timeout_seconds": timeout_seconds,
            "network_access": network_access,
            "env": dict(env),
            "tags": dict(tags),
        })
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError(self.fail_message)
        self.functions[function_name] = {
            "zip": zip_bytes,
            "timeout_seconds": timeout_seconds,
            "network_access": network_access,
            "env": dict(env),
            "tags": dict(tags),
        }
        return {
            "function_name": function_name,
            "function_arn": f"arn:aws:lambda:ca-central-1:000000000000:function:{function_name}",
        }

    async def invoke(self, *, function_name, payload, timeout_seconds, want_logs=False):
        self.invoke_count += 1
        data = self.functions.get(function_name)
        if data is None:
            raise RuntimeError(f"no function {function_name}")
        started = time.perf_counter()
        with tempfile.TemporaryDirectory() as tmp:
            with zipfile.ZipFile(io.BytesIO(data["zip"])) as zf:
                zf.extractall(tmp)
            sys.path.insert(0, tmp)
            for name in ("user_code", "handler"):
                sys.modules.pop(name, None)
            try:
                handler = importlib.import_module("handler")
                parsed = handler.lambda_handler(payload, None)
            finally:
                if sys.path and sys.path[0] == tmp:
                    sys.path.pop(0)
                for name in ("user_code", "handler"):
                    sys.modules.pop(name, None)
        return {
            "status_code": 200,
            "function_error": None,
            "payload": parsed if isinstance(parsed, dict) else None,
            "log_tail": "START\nEND\n" if want_logs else None,
            "duration_ms": int((time.perf_counter() - started) * 1000),
        }

    async def delete(self, *, function_name):
        self.functions.pop(function_name, None)

    async def list_function_names(self, *, prefix="setod-cs-"):
        return [name for name in self.functions if name.startswith(prefix)]

    async def function_env_tag(self, *, function_name):
        data = self.functions.get(function_name)
        if data is None:
            return None
        return (data.get("tags") or {}).get("setod-env")

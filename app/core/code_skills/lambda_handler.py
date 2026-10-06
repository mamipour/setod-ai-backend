"""Setod Code Skill wrapper. Entry point for every user function. Do not edit per-skill."""
import json
import traceback

MAX_OUTPUT_BYTES = 65_536


def _err(msg: str, tb: str | None = None) -> dict:
    out = {"ok": False, "error": msg[:2000]}
    if tb:
        out["traceback"] = tb[-2000:]
    return out


def lambda_handler(event, _lambda_context):
    try:
        import user_code  # noqa: WPS433 — user source, packaged alongside
    except Exception as exc:  # noqa: BLE001
        return _err(f"ImportError while loading user code: {type(exc).__name__}: {exc}", traceback.format_exc())

    main = getattr(user_code, "main", None)
    if not callable(main):
        return _err("user_code.main is not defined or not callable")

    payload_input = event.get("input", {}) if isinstance(event, dict) else {}
    payload_ctx = event.get("context", {}) if isinstance(event, dict) else {}

    try:
        result = main(payload_input, payload_ctx)
    except Exception as exc:  # noqa: BLE001
        return _err(f"{type(exc).__name__}: {exc}", traceback.format_exc())

    try:
        encoded = json.dumps(result, default=str)
    except Exception as exc:  # noqa: BLE001
        return _err(f"main() returned a value that is not JSON-serialisable: {exc}")

    if len(encoded.encode("utf-8")) > MAX_OUTPUT_BYTES:
        return {"ok": True, "result": encoded[:MAX_OUTPUT_BYTES] + "…[truncated]", "truncated": True}
    return {"ok": True, "result": result}

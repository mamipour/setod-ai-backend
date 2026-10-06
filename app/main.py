import logging
import logging.handlers
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from starlette.middleware.sessions import SessionMiddleware

from app.api.agents.router import router as agents_router
from app.api.approvals.router import router as approvals_router
from app.api.auth.router import router as auth_router
from app.api.connectors.router import router as connectors_router
from app.api.conversations.router import router as conversations_router
from app.api.notes.router import router as notes_router
from app.api.skills.router import router as skills_router
from app.api.tables.router import router as tables_router
from app.api.webhooks.router import router as webhooks_router
from app.api.hooks.router import router as hooks_router
from app.api.workspace.router import router as workspace_router
from app.api.billing.router import router as billing_router
from app.api.voice.router import router as voice_router
from app.api.code_skills.router import router as code_skills_router
from app.api.mcp.router import router as mcp_router
from app.config import settings
import app.db.models  # noqa: F401 — registers all SQLModel tables
from app.db.session import check_db
from app.core.billing.entitlements import EntitlementError
from app.limiter import limiter


_LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
logging.basicConfig(level=logging.INFO, format=_LOG_FORMAT)

# Dedicated rotating file for assist/copilot research traces — always written
# regardless of which terminal the server runs in.
_logs_dir = Path(__file__).resolve().parent.parent / "logs"
_logs_dir.mkdir(exist_ok=True)
_assist_fh = logging.handlers.RotatingFileHandler(
    _logs_dir / "assist.log",
    maxBytes=5 * 1024 * 1024,  # 5 MB per file
    backupCount=5,
    encoding="utf-8",
)
_assist_fh.setFormatter(logging.Formatter(_LOG_FORMAT))
_assist_fh.setLevel(logging.DEBUG)
logging.getLogger("setod.assist").addHandler(_assist_fh)
logging.getLogger("setod.assist").setLevel(logging.DEBUG)

# Quiet noisy libs; keep our own namespaces at INFO
logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


def _validate_production_config() -> None:
    """Raise at startup if critical production-only settings are missing.

    This prevents the server from starting with insecure defaults in production
    (e.g. a weak or default secret key, missing Stripe webhook secret).
    """
    if not settings.is_production:
        return

    errors: list[str] = []
    weak_secrets = {"secret", "changeme", "dev", "test", "development", "placeholder"}

    if not settings.app_secret_key or settings.app_secret_key.lower() in weak_secrets:
        errors.append("APP_SECRET_KEY is missing or insecure")

    if not settings.encryption_key:
        errors.append("ENCRYPTION_KEY is missing")

    if not settings.cors_origins:
        errors.append("CORS_ORIGINS is not set (no origins allowed in production)")

    if not settings.stripe_webhook_secret:
        errors.append("STRIPE_WEBHOOK_SECRET is not set")

    if not settings.google_client_id or not settings.google_client_secret:
        errors.append("GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET are not set")

    if settings.code_skills_enabled:
        missing = [
            name for name, value in (
                ("AWS_ACCESS_KEY_ID", settings.aws_access_key_id),
                ("AWS_SECRET_ACCESS_KEY", settings.aws_secret_access_key),
                ("AWS_USERCODE_DEPLOYER_ROLE_ARN", settings.aws_usercode_deployer_role_arn),
                ("AWS_USERCODE_EXEC_ROLE_ARN", settings.aws_usercode_exec_role_arn),
                ("AWS_USERCODE_EXTERNAL_ID", settings.aws_usercode_external_id),
                ("AWS_USERCODE_SUBNET_IDS", settings.aws_usercode_subnet_ids),
                ("AWS_USERCODE_SECURITY_GROUP_ID", settings.aws_usercode_security_group_id),
            )
            if not value
        ]
        if missing:
            errors.append("CODE_SKILLS_ENABLED is true but missing " + ", ".join(missing))

    if settings.mcp_server_enabled and not settings.mcp_public_url:
        errors.append("MCP_SERVER_ENABLED is true but MCP_PUBLIC_URL is empty")

    if errors:
        joined = "; ".join(errors)
        raise RuntimeError(
            f"Production startup blocked — fix the following config issues: {joined}"
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    _validate_production_config()
    await check_db()
    from app.core.code_skills.service import sweep_interrupted_deploys
    await sweep_interrupted_deploys()
    yield


app = FastAPI(
    title="Platform API",
    version="0.1.0",
    lifespan=lifespan,
    docs_url=None if settings.is_production else "/docs",
    redoc_url=None,
)

app.state.limiter = limiter
def _rate_limit_handler(request: Request, exc: RateLimitExceeded):
    # The JSON-RPC endpoint has to answer with a JSON-RPC error. Cookie routes keep slowapi's 429.
    if request.url.path == "/mcp":
        from fastapi.responses import JSONResponse
        return JSONResponse(
            {"jsonrpc": "2.0", "id": None, "error": {"code": -32029, "message": "Rate limited"}},
            status_code=200,
            headers={"MCP-Protocol-Version": "2025-03-26"},
        )
    return _rate_limit_exceeded_handler(request, exc)


app.add_exception_handler(RateLimitExceeded, _rate_limit_handler)
app.add_middleware(SlowAPIMiddleware)


# ── Security headers middleware ────────────────────────────────────────────────

@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault(
        "Permissions-Policy",
        "geolocation=(), microphone=(), camera=()",
    )
    # HSTS: enforce HTTPS for 1 year, include subdomains (production only).
    if settings.is_production:
        response.headers.setdefault(
            "Strict-Transport-Security",
            "max-age=31536000; includeSubDomains",
        )
    return response

# Session middleware required by Authlib OAuth state management
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.app_secret_key,
    same_site="lax",
    https_only=settings.is_production,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,  # required for cookies
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Routers ───────────────────────────────────────────────────────────────────

app.include_router(auth_router)
app.include_router(connectors_router)
app.include_router(agents_router)
app.include_router(approvals_router)
app.include_router(conversations_router)
app.include_router(notes_router)
app.include_router(skills_router)
app.include_router(code_skills_router)
app.include_router(tables_router)
app.include_router(webhooks_router)
app.include_router(hooks_router)
app.include_router(workspace_router)
app.include_router(billing_router)
app.include_router(voice_router)
app.include_router(mcp_router)


# ── Admin panel ───────────────────────────────────────────────────────────────
from app.admin.setup import create_admin  # noqa: E402
create_admin(app)


@app.exception_handler(EntitlementError)
async def entitlement_error_handler(request, exc: EntitlementError):
    from fastapi.responses import JSONResponse
    return JSONResponse(
        status_code=402,
        content={
            "feature": exc.feature,
            "plan_required": exc.plan_required,
            "upgrade_url": "/settings/plan",
            "message": exc.detail,
        },
    )


@app.get("/health")
async def health():
    return {"status": "ok"}



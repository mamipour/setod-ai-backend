import logging
import logging.handlers
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
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
from app.config import settings
import app.db.models  # noqa: F401 — registers all SQLModel tables
from app.db.session import check_db
from app.core.billing.entitlements import EntitlementError


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


@asynccontextmanager
async def lifespan(app: FastAPI):
    await check_db()
    yield


app = FastAPI(
    title="Platform API",
    version="0.1.0",
    lifespan=lifespan,
    docs_url=None if settings.is_production else "/docs",
    redoc_url=None,
)

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
app.include_router(tables_router)
app.include_router(webhooks_router)
app.include_router(hooks_router)
app.include_router(workspace_router)
app.include_router(billing_router)
app.include_router(voice_router)


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


@app.get("/admin-debug", include_in_schema=False)
async def admin_debug(request: Request):
    """Temporary: shows whether the access_token cookie reaches api.setod.com."""
    from app.config import settings as _s
    from app.api.auth.dependencies import decode_access_token
    from app.db.session import get_session as _gs
    token = request.cookies.get("access_token")
    if not token:
        return {"cookie": "MISSING", "cookies_received": list(request.cookies.keys())}
    try:
        user_id = decode_access_token(token)
    except Exception as e:
        return {"cookie": "INVALID", "error": str(e)}
    async for db in _gs():
        from app.db.models import User as _U
        user = await db.get(_U, user_id)
        if not user:
            return {"cookie": "ok", "user_id": str(user_id), "found": False}
        return {"cookie": "ok", "email": user.email, "is_staff": user.is_staff}

import logging
import logging.handlers
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware

from app.api.agents.router import router as agents_router
from app.api.approvals.router import router as approvals_router
from app.api.auth.router import router as auth_router
from app.api.connectors.router import router as connectors_router
from app.api.notes.router import router as notes_router
from app.api.skills.router import router as skills_router
from app.api.webhooks.router import router as webhooks_router
from app.api.workspace.router import router as workspace_router
from app.config import settings
import app.db.models  # noqa: F401 — registers all SQLModel tables
from app.db.session import check_db


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
app.include_router(notes_router)
app.include_router(skills_router)
app.include_router(webhooks_router)
app.include_router(workspace_router)


@app.get("/health")
async def health():
    return {"status": "ok"}

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
from app.config import settings
import app.db.models  # noqa: F401 — registers all SQLModel tables
from app.db.session import check_db


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


@app.get("/health")
async def health():
    return {"status": "ok"}

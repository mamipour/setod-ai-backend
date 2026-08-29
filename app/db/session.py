from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import settings

engine = create_async_engine(
    settings.database_url,
    echo=not settings.is_production,
    pool_pre_ping=True,
)

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def check_db() -> None:
    """Fail fast if the database is unreachable or migrations have not been applied.

    Schema creation belongs to Alembic (`alembic upgrade head`, which run.sh does on boot),
    not to the app — create_all would silently add tables that migrations know nothing about.
    """
    async with engine.begin() as conn:
        revision = await conn.exec_driver_sql(
            "select version_num from alembic_version"
        )
        if revision.scalar() is None:
            raise RuntimeError("No Alembic revision applied — run `alembic upgrade head`")


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        yield session

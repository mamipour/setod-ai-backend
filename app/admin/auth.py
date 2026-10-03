"""Custom sqladmin authentication backend using the existing JWT cookie."""
from typing import Optional

from fastapi.requests import Request
from fastapi.responses import RedirectResponse
from jose import JWTError, jwt
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession
from sqladmin.authentication import AuthenticationBackend

from app.config import settings
from app.db.models import User
from app.db.session import async_session_factory


class StaffAuthBackend(AuthenticationBackend):
    async def authenticate(self, request: Request) -> Optional[RedirectResponse]:
        """Called on every admin request.  Returns None if authenticated, redirect otherwise."""
        token = request.cookies.get("access_token")
        if not token:
            return RedirectResponse(request.url_for("admin:login"), status_code=302)
        try:
            payload = jwt.decode(token, settings.app_secret_key, algorithms=[settings.jwt_algorithm])
            user_id = payload["sub"]
        except (JWTError, KeyError):
            return RedirectResponse(request.url_for("admin:login"), status_code=302)

        async with async_session_factory() as db:
            user = await db.get(User, user_id)

        if not user or not user.is_staff:
            return RedirectResponse(request.url_for("admin:login"), status_code=302)

        return None

    async def login(self, request: Request) -> bool:
        # We don't have a separate admin login form; redirect to the app's Google login
        return False

    async def logout(self, request: Request) -> bool:
        return True

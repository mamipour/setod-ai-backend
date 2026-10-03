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
    def _login_redirect(self) -> RedirectResponse:
        """Send the user to the main app's Google login page."""
        frontend = settings.frontend_origin or "https://setod.com"
        return RedirectResponse(url=f"{frontend}/login", status_code=302)

    async def authenticate(self, request: Request) -> Optional[RedirectResponse]:
        """Called on every admin request.  Returns None if authenticated, redirect otherwise."""
        token = request.cookies.get("access_token")
        if not token:
            return self._login_redirect()
        try:
            payload = jwt.decode(token, settings.app_secret_key, algorithms=[settings.jwt_algorithm])
            user_id = payload["sub"]
        except (JWTError, KeyError):
            return self._login_redirect()

        async with async_session_factory() as db:
            user = await db.get(User, user_id)

        if not user:
            return self._login_redirect()

        if not user.is_staff:
            from starlette.responses import HTMLResponse
            return HTMLResponse(
                "<html><body style='font-family:sans-serif;padding:2rem'>"
                "<h2>403 — Forbidden</h2>"
                "<p>Your account does not have staff access to the admin panel.</p>"
                "<a href='https://setod.com/dashboard'>← Back to dashboard</a>"
                "</body></html>",
                status_code=403,
            )

        return True

    async def login(self, request: Request) -> bool:
        # The admin has no password form — the /admin/login GET page is never shown.
        # authenticate() above redirects straight to the main app Google login.
        return False

    async def logout(self, request: Request) -> bool:
        return True

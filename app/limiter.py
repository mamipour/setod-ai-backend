"""Shared rate-limiter instance.

Import this in routers to apply per-endpoint limits.  The limiter is
registered on the FastAPI app in app.main.

Example::

    from app.limiter import limiter

    @router.post("/login")
    @limiter.limit("20/minute")
    async def login(request: Request, ...):
        ...
"""
from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address, default_limits=["200/minute"])

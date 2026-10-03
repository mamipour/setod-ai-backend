"""sqladmin admin panel, mounted at /admin.  Only accessible to is_staff users."""
from app.admin.setup import admin

__all__ = ["admin"]

"""User-authored Python skills, deployed as one Lambda each. See CODE_SKILLS.md."""
from __future__ import annotations

from app.config import settings

_backend = None


class CodeSkillsDisabled(RuntimeError):
    """Raised when a caller asks AWS to do something while the feature is off."""


def get_backend():
    """The live Lambda backend. Tests monkeypatch this."""
    if not settings.code_skills_enabled:
        raise CodeSkillsDisabled("Code skills are not enabled on this deployment.")
    global _backend
    if _backend is None:
        from app.core.code_skills.aws import BotoLambdaBackend
        _backend = BotoLambdaBackend()
    return _backend

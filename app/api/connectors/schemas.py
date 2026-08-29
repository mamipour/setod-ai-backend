from datetime import datetime
from uuid import UUID

from pydantic import BaseModel

from app.db.models import ConnectorStatus, ConnectorType


class ConnectorOut(BaseModel):
    id: UUID
    name: str
    type: ConnectorType
    status: ConnectorStatus
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class TestResult(BaseModel):
    ok: bool
    detail: str

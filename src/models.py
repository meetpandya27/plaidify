from pydantic import BaseModel
from typing import Optional, Any

class ConnectRequest(BaseModel):
    site: str
    username: str
    password: str

class ConnectResponse(BaseModel):
    status: str
    data: Optional[dict[str, Any]] = None

class StatusResponse(BaseModel):
    status: str
    message: Optional[str] = None

class DisconnectResponse(BaseModel):
    status: str
    message: Optional[str] = None
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from turnoverops.core.config import Settings
from turnoverops.core.errors import AppError
from turnoverops.core.security import decode_token
from turnoverops.models import Host

bearer = HTTPBearer(auto_error=False)


def get_db(request: Request):
    db = request.app.state.session_factory()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_host(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    db: Annotated[Session, Depends(get_db)],
) -> Host:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise AppError(401, "unauthorized", "Missing bearer token")
    payload = decode_token(credentials.credentials, request.app.state.settings, purpose="access")
    host = db.get(Host, payload.get("sub"))
    if host is None:
        raise AppError(401, "unauthorized", "Invalid session")
    return host


Db = Annotated[Session, Depends(get_db)]
CurrentHost = Annotated[Host, Depends(get_host)]
AppSettings = Annotated[Settings, Depends(get_settings)]

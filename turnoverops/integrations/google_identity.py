from dataclasses import dataclass

from google.auth.exceptions import GoogleAuthError
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token

from turnoverops.core.errors import AppError


@dataclass(frozen=True)
class GoogleIdentity:
    sub: str
    email: str
    name: str
    picture: str | None


def verify_google_id_token(token: str, client_id: str) -> GoogleIdentity:
    if not client_id:
        raise AppError(503, "auth_unconfigured", "Google sign-in is not configured")
    try:
        info = id_token.verify_oauth2_token(token, google_requests.Request(), client_id)
    except (ValueError, GoogleAuthError) as exc:
        raise AppError(401, "invalid_token", "Google token was rejected") from exc
    if info.get("iss") not in {"accounts.google.com", "https://accounts.google.com"}:
        raise AppError(401, "invalid_token", "Google token was rejected")
    if info.get("email_verified") is not True:
        raise AppError(401, "email_unverified", "Google account email is not verified")
    subject = info.get("sub")
    email = info.get("email")
    if not subject or not email:
        raise AppError(401, "invalid_token", "Google token is missing identity claims")
    return GoogleIdentity(
        sub=str(subject),
        email=str(email),
        name=str(info.get("name") or email),
        picture=info.get("picture"),
    )

import pytest
from fastapi.testclient import TestClient

from turnoverops.app import create_app
from turnoverops.core.config import Settings
from turnoverops.core.errors import AppError
from turnoverops.integrations.google_identity import GoogleIdentity
from turnoverops.models import Base


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path}/test.db",
        jwt_secret="test-secret-that-is-long-enough-for-hs256",
        google_client_id="test-client",
        dev_mode=True,
        enable_scheduler=False,
        frontend_origin="http://localhost:5173",
        max_assignment_attempts=2,
        confirmation_timeout_minutes=60,
        notification_retry_seconds=60,
        max_notification_retries=3,
        telegram_webhook_secret="hook-secret",
        google_calendar_client_id="calendar-client",
        google_calendar_client_secret="calendar-secret",
    )


@pytest.fixture
def app(settings, monkeypatch):
    def verify(token: str, client_id: str) -> GoogleIdentity:
        if token.startswith("bad"):
            raise AppError(401, "invalid_token", "Google token was rejected")
        return GoogleIdentity(sub=token, email=f"{token}@example.com", name=token, picture=None)

    monkeypatch.setattr("turnoverops.integrations.google_identity.verify_google_id_token", verify)
    application = create_app(settings)
    Base.metadata.create_all(application.state.engine)
    yield application
    application.state.engine.dispose()


@pytest.fixture
def client(app):
    with TestClient(app) as test_client:
        yield test_client


def login(client, subject: str = "ada") -> dict:
    response = client.post("/auth/google", json={"id_token": subject})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def create_property(client, headers, **overrides) -> dict:
    body = {
        "name": "Harbor",
        "timezone": "UTC",
        "default_checkout_time": "10:00:00",
        "default_checkin_time": "15:00:00",
        "cleaning_duration_minutes": 60,
        "feed_kind": "demo",
    }
    body.update(overrides)
    response = client.post("/properties", json=body, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


def create_cleaner(client, headers, name: str, **overrides) -> dict:
    body = {
        "name": name,
        "email": f"{name.lower().replace(' ', '.')}@example.com",
        "timezone": "UTC",
        "working_days": [0, 1, 2, 3, 4, 5, 6],
        "work_start": "09:00:00",
        "work_end": "17:00:00",
    }
    body.update(overrides)
    response = client.post("/cleaners", json=body, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


def sync(client, headers, property_id: str, reservations: list[dict]) -> dict:
    response = client.post(f"/properties/{property_id}/sync", json={"reservations": reservations}, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def list_tasks(client, headers, property_id: str) -> list[dict]:
    response = client.get("/tasks", params={"property_id": property_id, "limit": 100}, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["items"]


def task_by_key(tasks: list[dict], key: str) -> dict:
    return next(item for item in tasks if item["turnover_key"] == key)


STAYS = [
    {"external_uid": "stay-1", "start": "2026-11-01T15:00:00Z", "end": "2026-11-03T10:00:00Z"},
    {"external_uid": "stay-2", "start": "2026-11-03T15:00:00Z", "end": "2026-11-05T10:00:00Z"},
]

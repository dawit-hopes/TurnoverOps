import socket

import httpx
import pytest

from turnoverops.integrations.demo_feed import parse_demo_feed
from turnoverops.integrations.ical_feed import fetch_feed, parse_ical, validate_public_http_url
from turnoverops.integrations.snapshots import FeedError
from zoneinfo import ZoneInfo
from datetime import time

from tests.conftest import STAYS, create_property, list_tasks, login, sync

ICAL = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:stay-1
DTSTART:20261101T150000Z
DTEND:20261103T100000Z
SUMMARY:Stay
END:VEVENT
BEGIN:VEVENT
UID:stay-2
DTSTART:20261103T150000Z
DTEND:20261105T100000Z
END:VEVENT
END:VCALENDAR
"""


def test_repeated_demo_sync_does_not_duplicate(client):
    headers = login(client)
    prop = create_property(client, headers)
    first = sync(client, headers, prop["id"], STAYS)
    second = sync(client, headers, prop["id"], STAYS)
    reservations = client.get("/reservations", params={"property_id": prop["id"]}, headers=headers).json()
    assert first["created"] == 2
    assert second["created"] == 0
    assert second["updated"] == 0
    assert reservations["total"] == 2
    assert len(list_tasks(client, headers, prop["id"])) == 2


def test_ical_text_matches_demo_snapshots():
    snapshots = parse_ical(ICAL, zone=ZoneInfo("UTC"), default_checkin=time(15, 0), default_checkout=time(10, 0))
    demo = parse_demo_feed({"reservations": STAYS})
    assert [(item.external_uid, item.start_at, item.end_at) for item in snapshots] == [
        (item.external_uid, item.start_at, item.end_at) for item in demo
    ]


def test_missing_reservation_is_cancelled(client):
    headers = login(client)
    prop = create_property(client, headers)
    sync(client, headers, prop["id"], STAYS)
    sync(client, headers, prop["id"], STAYS[:1])
    rows = client.get("/reservations", params={"property_id": prop["id"], "include_cancelled": True}, headers=headers).json()
    cancelled = {row["external_uid"]: row["cancelled"] for row in rows["items"]}
    assert cancelled["stay-2"] is True
    assert cancelled["stay-1"] is False


def test_private_addresses_and_redirects_are_rejected():
    for url in ("http://127.0.0.1/feed.ics", "http://10.1.1.1/feed.ics", "http://localhost/feed.ics", "file:///tmp/feed.ics"):
        with pytest.raises(FeedError):
            validate_public_http_url(url)

    def resolve_private(host, port):
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", port))]

    with pytest.raises(FeedError):
        validate_public_http_url("http://example.com/feed.ics", resolve=resolve_private)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "1.1.1.1":
            return httpx.Response(302, headers={"location": "http://127.0.0.1/secret"})
        return httpx.Response(200, text="nope")

    with pytest.raises(FeedError):
        fetch_feed(
            "http://1.1.1.1/feed.ics",
            timeout=2,
            max_bytes=1000,
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )


def test_failed_feed_fetch_leaves_tasks_unchanged(client, monkeypatch):
    headers = login(client)
    prop = create_property(client, headers)
    sync(client, headers, prop["id"], STAYS)
    before = list_tasks(client, headers, prop["id"])
    updated = client.patch(
        f"/properties/{prop['id']}",
        json={"feed_kind": "ical", "ical_url": "http://1.1.1.1/feed.ics"},
        headers=headers,
    )
    assert updated.status_code == 200, updated.text

    def explode(*_args, **_kwargs):
        raise FeedError("feed unavailable")

    monkeypatch.setattr("turnoverops.integrations.ical_feed.fetch_feed", explode)
    response = client.post(f"/properties/{prop['id']}/sync", json={}, headers=headers)
    assert response.status_code == 200
    assert response.json()["sync_status"] == "error"
    assert list_tasks(client, headers, prop["id"]) == before

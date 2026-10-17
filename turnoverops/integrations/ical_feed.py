import ipaddress
import socket
from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

import httpx
from icalendar import Calendar

from turnoverops.integrations.snapshots import FeedError, ReservationSnapshot
from turnoverops.scheduling.windows import as_utc

Resolver = Callable[[str, int], list]


def default_resolve(host: str, port: int) -> list:
    return socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)


def validate_public_http_url(url: str, *, resolve: Resolver | None = None) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise FeedError("Only http and https feeds are allowed")
    if parsed.username or parsed.password:
        raise FeedError("Feed URL must not include credentials")
    host = parsed.hostname
    if host is None:
        raise FeedError("Feed URL is missing a host")
    lowered = host.lower().rstrip(".")
    if lowered == "localhost" or lowered.endswith((".localhost", ".local", ".internal")):
        raise FeedError("Feed URL host is not allowed")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        addresses = [ipaddress.ip_address(lowered)]
    except ValueError:
        try:
            infos = (resolve or default_resolve)(lowered, port)
        except socket.gaierror as exc:
            raise FeedError("Feed URL could not be resolved") from exc
        addresses = []
        for info in infos:
            addresses.append(ipaddress.ip_address(info[4][0]))
    if not addresses:
        raise FeedError("Feed URL did not resolve")
    for address in addresses:
        if not address.is_global:
            raise FeedError("Feed URL resolves to a disallowed address")


def fetch_feed(
    url: str,
    *,
    timeout: float,
    max_bytes: int,
    client: httpx.Client | None = None,
    resolve: Resolver | None = None,
) -> str:
    owns_client = client is None
    http = client or httpx.Client()
    current = url
    try:
        for _ in range(4):
            validate_public_http_url(current, resolve=resolve)
            try:
                response = http.get(current, follow_redirects=False, timeout=timeout)
            except httpx.HTTPError as exc:
                raise FeedError("Feed request failed") from exc
            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("location")
                if not location:
                    raise FeedError("Redirect is missing a location")
                current = urljoin(current, location)
                continue
            if response.status_code >= 400:
                raise FeedError(f"Feed request failed with status {response.status_code}")
            if len(response.content) > max_bytes:
                raise FeedError("Feed response is too large")
            return response.text
        raise FeedError("Too many redirects")
    finally:
        if owns_client:
            http.close()


def _event_datetime(value: datetime | date, zone: ZoneInfo, clock: time) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return as_utc(value.replace(tzinfo=zone))
        return as_utc(value)
    local = datetime.combine(value, clock).replace(tzinfo=zone)
    return as_utc(local)


def parse_ical(
    text: str,
    *,
    zone: ZoneInfo,
    default_checkin: time,
    default_checkout: time,
) -> list[ReservationSnapshot]:
    try:
        calendar = Calendar.from_ical(text)
    except ValueError as exc:
        raise FeedError("Calendar feed could not be parsed") from exc
    snapshots: list[ReservationSnapshot] = []
    seen: set[str] = set()
    for component in calendar.walk("VEVENT"):
        raw_uid = component.get("uid")
        if raw_uid is None:
            continue
        uid = str(raw_uid).strip()
        if not uid or len(uid) > 255 or uid in seen:
            if uid in seen:
                raise FeedError(f"Duplicate reservation id {uid}")
            continue
        try:
            start_value = component.decoded("dtstart")
            end_value = component.decoded("dtend")
        except KeyError:
            continue
        start_at = _event_datetime(start_value, zone, default_checkin)
        end_at = _event_datetime(end_value, zone, default_checkout)
        if isinstance(start_value, date) and not isinstance(start_value, datetime) and end_at <= start_at:
            end_at = end_at + timedelta(days=1)
        if end_at <= start_at:
            continue
        status = str(component.get("status") or "").upper()
        summary = component.get("summary")
        seen.add(uid)
        snapshots.append(
            ReservationSnapshot(
                external_uid=uid,
                start_at=start_at,
                end_at=end_at,
                cancelled=status == "CANCELLED",
                summary=str(summary)[:300] if summary else None,
                source="ical",
            )
        )
    return snapshots

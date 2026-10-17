from turnoverops.integrations.snapshots import FeedError, ReservationSnapshot
from turnoverops.scheduling.windows import parse_aware


def parse_demo_feed(payload: dict, *, source: str = "demo") -> list[ReservationSnapshot]:
    if not isinstance(payload, dict) or not isinstance(payload.get("reservations"), list):
        raise FeedError("Demo feed must be an object with a reservations list")
    snapshots: list[ReservationSnapshot] = []
    seen: set[str] = set()
    for item in payload["reservations"]:
        if not isinstance(item, dict):
            raise FeedError("Each reservation must be an object")
        uid = str(item.get("external_uid") or "").strip()
        if not uid or len(uid) > 255:
            raise FeedError("Each reservation needs an external_uid of at most 255 characters")
        if uid in seen:
            raise FeedError(f"Duplicate reservation id {uid}")
        seen.add(uid)
        try:
            start_at = parse_aware(item["start"])
            end_at = parse_aware(item["end"])
        except (KeyError, TypeError, ValueError) as exc:
            raise FeedError(f"Reservation {uid} has an invalid start or end") from exc
        if end_at <= start_at:
            raise FeedError(f"Reservation {uid} must end after it starts")
        summary = item.get("summary")
        snapshots.append(
            ReservationSnapshot(
                external_uid=uid,
                start_at=start_at,
                end_at=end_at,
                cancelled=bool(item.get("cancelled", False)),
                summary=str(summary)[:300] if summary else None,
                source=source,
            )
        )
    return snapshots

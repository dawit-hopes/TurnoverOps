import hashlib
from dataclasses import dataclass
from datetime import datetime


class FeedError(Exception):
    pass


@dataclass(frozen=True)
class ReservationSnapshot:
    external_uid: str
    start_at: datetime
    end_at: datetime
    cancelled: bool
    summary: str | None
    source: str
    guest_name: str | None = None

    def digest(self) -> str:
        raw = "|".join(
            [
                self.external_uid,
                self.start_at.isoformat(),
                self.end_at.isoformat(),
                str(self.cancelled),
                self.summary or "",
                self.guest_name or "",
                self.source,
            ]
        )
        return hashlib.sha256(raw.encode()).hexdigest()

from sqlalchemy.orm import Session

from turnoverops.core.context import get_correlation_id
from turnoverops.models import AuditEvent


def audit(
    db: Session,
    *,
    host_id: str,
    event_type: str,
    entity_type: str,
    entity_id: str,
    reason: str,
    details: dict | None = None,
) -> AuditEvent:
    event = AuditEvent(
        host_id=host_id,
        event_type=event_type,
        entity_type=entity_type,
        entity_id=entity_id,
        reason=reason[:500],
        details=details,
        correlation_id=get_correlation_id(),
    )
    db.add(event)
    return event

from sqlalchemy.orm import Session

from turnoverops.core.errors import not_found


def owned(db: Session, model: type, host_id: str, object_id: str):
    row = db.get(model, object_id)
    if row is None or row.host_id != host_id:
        raise not_found()
    return row

from sqlalchemy import func, select
from sqlalchemy.orm import Session


def window(db: Session, stmt, limit: int, offset: int):
    total = db.scalar(select(func.count()).select_from(stmt.order_by(None).subquery())) or 0
    rows = db.scalars(stmt.limit(limit).offset(offset)).all()
    return total, rows

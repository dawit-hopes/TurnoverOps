from sqlalchemy import select
from sqlalchemy.orm import Session

from turnoverops.integrations.google_identity import GoogleIdentity
from turnoverops.models import Host


def upsert_host(db: Session, identity: GoogleIdentity) -> Host:
    host = db.scalar(select(Host).where(Host.google_sub == identity.sub))
    if host is None:
        host = Host(google_sub=identity.sub, email=identity.email, name=identity.name, picture=identity.picture)
        db.add(host)
        db.flush()
        return host
    host.email = identity.email
    host.name = identity.name
    host.picture = identity.picture
    return host

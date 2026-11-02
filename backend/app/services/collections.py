from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import Collection, Document

DEFAULT_NAME = "General"


class CollectionError(Exception):
    """A collection operation the system refuses; the message is safe to show."""


def get_collection(db: Session, *, tenant_id: UUID, collection_id: UUID) -> Collection | None:
    collection = db.get(Collection, collection_id)
    return collection if collection is not None and collection.tenant_id == tenant_id else None


def default_collection(db: Session, *, tenant_id: UUID) -> Collection:
    """The tenant's catch-all collection; created on first use."""
    existing = db.scalars(
        select(Collection).where(Collection.tenant_id == tenant_id, Collection.is_default.is_(True))
    ).first()
    if existing is not None:
        return existing
    collection = Collection(
        tenant_id=tenant_id, name=DEFAULT_NAME, description="Default knowledge", is_default=True
    )
    db.add(collection)
    db.commit()
    db.refresh(collection)
    return collection


def list_collections(db: Session, *, tenant_id: UUID) -> list[tuple[Collection, int]]:
    counts = dict(
        db.execute(
            select(Document.collection_id, func.count())
            .where(Document.tenant_id == tenant_id, Document.collection_id.is_not(None))
            .group_by(Document.collection_id)
        ).all()
    )
    collections = db.scalars(
        select(Collection).where(Collection.tenant_id == tenant_id).order_by(Collection.created_at)
    )
    return [(c, counts.get(c.id, 0)) for c in collections]


def create_collection(db: Session, *, tenant_id: UUID, name: str, description: str) -> Collection:
    name = name.strip()
    taken = db.scalars(
        select(Collection).where(
            Collection.tenant_id == tenant_id, func.lower(Collection.name) == name.lower()
        )
    ).first()
    if taken is not None:
        raise CollectionError("A collection with that name already exists")
    collection = Collection(tenant_id=tenant_id, name=name, description=description.strip())
    db.add(collection)
    db.commit()
    db.refresh(collection)
    return collection


def update_collection(
    db: Session, *, collection: Collection, name: str | None, description: str | None
) -> Collection:
    if name is not None and name.strip().lower() != collection.name.lower():
        taken = db.scalars(
            select(Collection).where(
                Collection.tenant_id == collection.tenant_id,
                func.lower(Collection.name) == name.strip().lower(),
                Collection.id != collection.id,
            )
        ).first()
        if taken is not None:
            raise CollectionError("A collection with that name already exists")
        collection.name = name.strip()
    if description is not None:
        collection.description = description.strip()
    db.commit()
    db.refresh(collection)
    return collection


def delete_collection(db: Session, *, collection: Collection) -> None:
    if collection.is_default:
        raise CollectionError("The default collection can't be deleted")
    in_use = db.scalar(
        select(func.count()).select_from(Document).where(Document.collection_id == collection.id)
    )
    if in_use:
        raise CollectionError("Move or delete the documents in this collection first")
    db.delete(collection)
    db.commit()

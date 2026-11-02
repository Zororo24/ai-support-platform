from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.deps import require_roles
from app.api.schemas.agents import CollectionCreate, CollectionOut, CollectionPatch
from app.db.models import User, UserRole
from app.db.session import get_db
from app.services import audit
from app.services import collections as svc

router = APIRouter(prefix="/admin/collections", tags=["knowledge"])

admin_only = require_roles(UserRole.admin)


def _out(collection, count: int) -> CollectionOut:
    return CollectionOut(
        id=collection.id,
        name=collection.name,
        description=collection.description,
        is_default=collection.is_default,
        document_count=count,
    )


def _require(db: Session, user: User, collection_id: UUID):
    collection = svc.get_collection(db, tenant_id=user.tenant_id, collection_id=collection_id)
    if collection is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Collection not found")
    return collection


@router.get("", response_model=list[CollectionOut])
def list_collections(user: User = Depends(admin_only), db: Session = Depends(get_db)) -> list[CollectionOut]:
    svc.default_collection(db, tenant_id=user.tenant_id)  # make sure the catch-all exists
    return [_out(c, n) for c, n in svc.list_collections(db, tenant_id=user.tenant_id)]


@router.post("", response_model=CollectionOut, status_code=status.HTTP_201_CREATED)
def create_collection(
    body: CollectionCreate, user: User = Depends(admin_only), db: Session = Depends(get_db)
) -> CollectionOut:
    try:
        collection = svc.create_collection(
            db, tenant_id=user.tenant_id, name=body.name, description=body.description
        )
    except svc.CollectionError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    audit.record(
        db, actor=user, action="collection.create", target_type="collection", target_id=collection.id,
        detail={"name": collection.name},
    )  # fmt: skip
    return _out(collection, 0)


@router.patch("/{collection_id}", response_model=CollectionOut)
def patch_collection(
    collection_id: UUID,
    body: CollectionPatch,
    user: User = Depends(admin_only),
    db: Session = Depends(get_db),
) -> CollectionOut:
    collection = _require(db, user, collection_id)
    try:
        svc.update_collection(db, collection=collection, name=body.name, description=body.description)
    except svc.CollectionError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    counts = {c.id: n for c, n in svc.list_collections(db, tenant_id=user.tenant_id)}
    return _out(collection, counts.get(collection.id, 0))


@router.delete("/{collection_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_collection(
    collection_id: UUID, user: User = Depends(admin_only), db: Session = Depends(get_db)
) -> None:
    collection = _require(db, user, collection_id)
    name = collection.name
    try:
        svc.delete_collection(db, collection=collection)
    except svc.CollectionError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    audit.record(
        db, actor=user, action="collection.delete", target_type="collection", target_id=collection_id,
        detail={"name": name},
    )  # fmt: skip

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, require_roles
from app.api.schemas.tickets import (
    BulkStatusIn,
    BulkStatusOut,
    CommentIn,
    CommentOut,
    TicketCreate,
    TicketDetail,
    TicketList,
    TicketOut,
    TicketPatch,
)
from app.db.models import TicketPriority, TicketStatus, User, UserRole
from app.db.session import get_db
from app.services import audit
from app.services import tickets as tickets_service

router = APIRouter(prefix="/tickets", tags=["tickets"])

admin_only = require_roles(UserRole.admin)


def _require_ticket(db: Session, user: User, ticket_id: UUID):
    ticket = tickets_service.get_ticket(db, user=user, ticket_id=ticket_id)
    if ticket is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ticket not found")
    return ticket


@router.post("", response_model=TicketOut, status_code=status.HTTP_201_CREATED)
def create_ticket(
    body: TicketCreate, user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> TicketOut:
    ticket = tickets_service.create_ticket(
        db, user=user, subject=body.subject, body=body.body, priority=body.priority
    )
    return tickets_service.to_out(db, [ticket])[0]


@router.get("", response_model=TicketList)
def list_tickets(
    status_filter: TicketStatus | None = Query(default=None, alias="status"),
    priority: TicketPriority | None = None,
    assignee: str | None = Query(default=None, description="unassigned | me | <user id>"),
    q: str | None = Query(default=None, max_length=200),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> TicketList:
    if assignee not in (None, "unassigned", "me"):
        try:
            UUID(assignee)
        except ValueError:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid assignee")
    tickets, total = tickets_service.list_tickets(
        db,
        user=user,
        status=status_filter,
        priority=priority,
        assignee=assignee,
        q=q,
        limit=limit,
        offset=offset,
    )
    return TicketList(items=tickets_service.to_out(db, tickets), total=total)


@router.post("/bulk-status", response_model=BulkStatusOut)
def bulk_status(
    body: BulkStatusIn, user: User = Depends(admin_only), db: Session = Depends(get_db)
) -> BulkStatusOut:
    count = tickets_service.bulk_set_status(db, user=user, ids=body.ids, status=body.status)
    audit.record(
        db,
        actor=user,
        action="ticket.bulk_status",
        target_type="ticket",
        detail={"status": body.status.value, "requested": len(body.ids), "updated": count},
    )
    return BulkStatusOut(updated=count)


@router.get("/{ticket_id}", response_model=TicketDetail)
def get_ticket(
    ticket_id: UUID, user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> TicketDetail:
    ticket = _require_ticket(db, user, ticket_id)
    base = tickets_service.to_out(db, [ticket])[0]
    return TicketDetail(**base.model_dump(), comments=tickets_service.list_comments(db, ticket=ticket))


@router.patch("/{ticket_id}", response_model=TicketOut)
def patch_ticket(
    ticket_id: UUID,
    body: TicketPatch,
    user: User = Depends(admin_only),
    db: Session = Depends(get_db),
) -> TicketOut:
    ticket = _require_ticket(db, user, ticket_id)
    before = {
        "status": ticket.status.value,
        "priority": ticket.priority.value,
        "assignee_id": str(ticket.assignee_id) if ticket.assignee_id else None,
    }
    try:
        ticket = tickets_service.update_ticket(
            db,
            ticket=ticket,
            status=body.status,
            priority=body.priority,
            set_assignee="assignee_id" in body.model_fields_set,
            assignee_id=body.assignee_id,
        )
    except tickets_service.InvalidAssignee:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Assignee must be an active admin in your organisation",
        )
    after = {
        "status": ticket.status.value,
        "priority": ticket.priority.value,
        "assignee_id": str(ticket.assignee_id) if ticket.assignee_id else None,
    }
    changes = {k: {"from": before[k], "to": after[k]} for k in after if before[k] != after[k]}
    if changes:
        audit.record(
            db, actor=user, action="ticket.update", target_type="ticket", target_id=ticket.id, detail=changes
        )
    return tickets_service.to_out(db, [ticket])[0]


@router.post("/{ticket_id}/comments", response_model=CommentOut, status_code=status.HTTP_201_CREATED)
def add_comment(
    ticket_id: UUID,
    body: CommentIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> CommentOut:
    ticket = _require_ticket(db, user, ticket_id)
    if not body.body.strip():
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Comment is empty")
    comment = tickets_service.add_comment(db, user=user, ticket=ticket, body=body.body)
    return tickets_service.comment_out(db, comment)

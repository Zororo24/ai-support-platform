from uuid import UUID

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from app.api.schemas.tickets import CommentOut, TicketOut
from app.db.models import (
    Ticket,
    TicketComment,
    TicketPriority,
    TicketStatus,
    User,
    UserRole,
)


class InvalidAssignee(Exception):
    pass


def create_ticket(
    db: Session,
    *,
    user: User,
    subject: str,
    body: str,
    conversation_id: UUID | None = None,
    priority: TicketPriority = TicketPriority.normal,
) -> Ticket:
    ticket = Ticket(
        tenant_id=user.tenant_id,
        user_id=user.id,
        subject=subject[:300],
        body=body,
        status=TicketStatus.open,
        priority=priority,
        conversation_id=conversation_id,
    )
    db.add(ticket)
    db.commit()
    db.refresh(ticket)
    return ticket


def _visible(user: User):
    """Base WHERE clauses: tenant always; customers only their own tickets."""
    clauses = [Ticket.tenant_id == user.tenant_id]
    if user.role != UserRole.admin:
        clauses.append(Ticket.user_id == user.id)
    return clauses


def list_tickets(
    db: Session,
    *,
    user: User,
    status: TicketStatus | None = None,
    priority: TicketPriority | None = None,
    assignee: str | None = None,  # "unassigned" | "me" | a user id (admins only)
    q: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[Ticket], int]:
    clauses = _visible(user)
    if status is not None:
        clauses.append(Ticket.status == status)
    if priority is not None:
        clauses.append(Ticket.priority == priority)
    if assignee and user.role == UserRole.admin:
        if assignee == "unassigned":
            clauses.append(Ticket.assignee_id.is_(None))
        elif assignee == "me":
            clauses.append(Ticket.assignee_id == user.id)
        else:
            clauses.append(Ticket.assignee_id == UUID(assignee))
    if q:
        like = f"%{q.strip()}%"
        clauses.append(or_(Ticket.subject.ilike(like), Ticket.body.ilike(like)))
    total = db.scalar(select(func.count()).select_from(Ticket).where(*clauses)) or 0
    stmt = select(Ticket).where(*clauses).order_by(Ticket.created_at.desc()).limit(limit).offset(offset)
    return list(db.scalars(stmt)), total


def get_ticket(db: Session, *, user: User, ticket_id: UUID) -> Ticket | None:
    ticket = db.get(Ticket, ticket_id)
    if ticket is None or ticket.tenant_id != user.tenant_id:
        return None
    if user.role != UserRole.admin and ticket.user_id != user.id:
        return None
    return ticket


def update_ticket(
    db: Session,
    *,
    ticket: Ticket,
    status: TicketStatus | None,
    priority: TicketPriority | None,
    set_assignee: bool,
    assignee_id: UUID | None,
) -> Ticket:
    if set_assignee:
        if assignee_id is not None:
            assignee = db.get(User, assignee_id)
            if (
                assignee is None
                or assignee.tenant_id != ticket.tenant_id
                or assignee.role != UserRole.admin
                or not assignee.is_active
            ):
                raise InvalidAssignee
        ticket.assignee_id = assignee_id
    if status is not None:
        ticket.status = status
    if priority is not None:
        ticket.priority = priority
    db.commit()
    db.refresh(ticket)
    return ticket


def bulk_set_status(db: Session, *, user: User, ids: list[UUID], status: TicketStatus) -> int:
    """Admin bulk update. Ids from other tenants are silently ignored, never touched."""
    result = db.execute(
        update(Ticket).where(Ticket.tenant_id == user.tenant_id, Ticket.id.in_(ids)).values(status=status)
    )
    db.commit()
    return result.rowcount or 0


def add_comment(db: Session, *, user: User, ticket: Ticket, body: str) -> TicketComment:
    comment = TicketComment(
        tenant_id=ticket.tenant_id, ticket_id=ticket.id, author_id=user.id, body=body.strip()
    )
    db.add(comment)
    # A customer replying means the ball is back with the support team.
    if user.role != UserRole.admin and ticket.status != TicketStatus.open:
        ticket.status = TicketStatus.open
    db.commit()
    db.refresh(comment)
    return comment


def list_comments(db: Session, *, ticket: Ticket) -> list[CommentOut]:
    rows = db.execute(
        select(TicketComment, User.email, User.role)
        .join(User, User.id == TicketComment.author_id)
        .where(TicketComment.tenant_id == ticket.tenant_id, TicketComment.ticket_id == ticket.id)
        .order_by(TicketComment.created_at)
    ).all()
    return [
        CommentOut(id=c.id, author_email=email, author_role=role, body=c.body, created_at=c.created_at)
        for c, email, role in rows
    ]


def comment_out(db: Session, comment: TicketComment) -> CommentOut:
    author = db.get(User, comment.author_id)
    return CommentOut(
        id=comment.id,
        author_email=author.email,
        author_role=author.role,
        body=comment.body,
        created_at=comment.created_at,
    )


def to_out(db: Session, tickets: list[Ticket]) -> list[TicketOut]:
    """Build API objects, resolving requester/assignee emails in one query."""
    ids = {t.user_id for t in tickets} | {t.assignee_id for t in tickets if t.assignee_id}
    emails = {}
    if ids:
        emails = {r.id: r.email for r in db.execute(select(User.id, User.email).where(User.id.in_(ids)))}
    return [
        TicketOut(
            id=t.id,
            subject=t.subject,
            body=t.body,
            status=t.status,
            priority=t.priority,
            requester_email=emails.get(t.user_id),
            assignee_id=t.assignee_id,
            assignee_email=emails.get(t.assignee_id) if t.assignee_id else None,
            conversation_id=t.conversation_id,
            created_at=t.created_at,
            updated_at=t.updated_at,
        )
        for t in tickets
    ]

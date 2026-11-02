from uuid import UUID

from sqlalchemy.orm import Session

from app.db.models import Ticket, User
from app.services import tickets as tickets_service


def create_ticket(
    db: Session, *, user: User, subject: str, body: str, conversation_id: UUID | None = None
) -> Ticket:
    return tickets_service.create_ticket(
        db, user=user, subject=subject, body=body, conversation_id=conversation_id
    )

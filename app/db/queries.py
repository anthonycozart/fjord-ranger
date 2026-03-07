"""
Common database queries shared across jobs and handlers.
"""

from sqlalchemy.orm import Session, joinedload

from app.db.models import User, UserCriteria, UserSlotState


def get_bulk_release_users(db: Session) -> list[User]:
    """
    Return active users who have opted into bulk release notifications.

    Checks for criteria["bulk_release_alerts"] == true (JSON boolean).
    Uses joinedload so user.criteria remains accessible after session closes.
    """
    return (
        db.query(User)
        .join(UserCriteria, User.id == UserCriteria.user_id)
        .options(joinedload(User.criteria))
        .filter(
            User.status == "active",
            UserCriteria.criteria["bulk_release_alerts"].astext == "true",
        )
        .all()
    )


def get_notifiable_users(db: Session) -> list[User]:
    """
    Return active users who haven't hit their daily cap.

    Includes users with no criteria — they match all slots and will receive
    a preferences nudge after their first notification.

    Uses outerjoin + joinedload so user.criteria is accessible (possibly None)
    after the session closes.
    """
    return (
        db.query(User)
        .outerjoin(UserCriteria, User.id == UserCriteria.user_id)
        .options(joinedload(User.criteria))
        .filter(
            User.status == "active",
            User.daily_notification_count < User.max_notifications_per_day,
        )
        .all()
    )


def get_user_slot_state(
    db: Session, user_id: int, momence_id: int
) -> UserSlotState | None:
    """Return the existing state row for a (user, slot) pair, or None."""
    return (
        db.query(UserSlotState)
        .filter_by(user_id=user_id, momence_id=momence_id)
        .first()
    )


def create_user_slot_state(
    db: Session, user_id: int, momence_id: int, state: str = "new"
) -> UserSlotState:
    """Insert a new user_slot_states row and return it."""
    row = UserSlotState(user_id=user_id, momence_id=momence_id, state=state)
    db.add(row)
    db.flush()  # get the row into the session without committing
    return row

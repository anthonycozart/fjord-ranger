"""
Nightly janitor job.

Runs at midnight PT via APScheduler. Two tasks:

  1. Reset daily_notification_count — zeroes each user's counter so the
     daily cap resets for a new day. Updates daily_count_reset_at so the
     same user isn't reset twice if the janitor somehow runs more than once.

  2. Expire past slot states — marks user_slot_states as 'expired' for any
     slot whose starts_at has already passed and whose state is still 'new'
     or 'notified'. Keeps the state machine clean and prevents stale rows
     from blocking re-notification if a slot somehow reappears.

Both tasks run in a single DB transaction — either both commit or neither does.
"""

import asyncio
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.db.database import get_session_factory
from app.db.models import Slot, User, UserSlotState

logger = logging.getLogger(__name__)

PT = ZoneInfo("America/Los_Angeles")


async def run_janitor() -> None:
    """Entry point called by APScheduler at midnight PT."""
    logger.info("Janitor starting")
    try:
        await asyncio.to_thread(_run_sync)
    except Exception as e:
        logger.error("Janitor failed: %s: %s", type(e).__name__, e)


def _run_sync() -> None:
    db = get_session_factory()()
    try:
        counts_reset = _reset_daily_counts(db)
        slots_expired = _expire_past_slots(db)
        db.commit()
        logger.info(
            "Janitor complete: reset %d daily counter(s), expired %d slot state(s)",
            counts_reset,
            slots_expired,
        )
    except Exception as e:
        db.rollback()
        raise
    finally:
        db.close()


def _reset_daily_counts(db: Session) -> int:
    """
    Zero out daily_notification_count for users whose counter hasn't been
    reset today (PT). Updates daily_count_reset_at to today so we don't
    reset them again if the janitor runs a second time on the same day.

    Returns the number of users reset.
    """
    today_pt = datetime.now(PT).date()
    users = (
        db.query(User)
        .filter(User.daily_count_reset_at < today_pt)
        .all()
    )
    for user in users:
        user.daily_notification_count = 0
        user.daily_count_reset_at = today_pt
    return len(users)


def _expire_past_slots(db: Session) -> int:
    """
    Mark user_slot_states as 'expired' where the corresponding slot's
    starts_at has passed and the state is still 'new' or 'notified'.

    'new'      — we knew about the slot but never sent a notification
                 (user hit daily cap, or was added after the slot appeared)
    'notified' — we sent a notification but the user never replied

    Both are dead ends once the slot is in the past.

    Returns the number of states expired.
    """
    now = datetime.now(timezone.utc)
    states = (
        db.query(UserSlotState)
        .join(Slot, UserSlotState.momence_id == Slot.momence_id)
        .filter(
            UserSlotState.state.in_(["new", "notified"]),
            Slot.starts_at <= now,
        )
        .all()
    )
    for state in states:
        state.state = "expired"
    return len(states)

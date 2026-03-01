"""
Slot sync: upserts scraped MomenceSession objects into the slots table
and returns which slots are newly available (the ones worth notifying about).

A slot is "newly available" if it currently has spots AND either:
  - It was never seen before (brand new slot), OR
  - It was previously full or cancelled (a cancellation just opened it up)

This distinction is the core value of the service — catching cancellations
the moment they appear.
"""

import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.db.models import Slot
from app.scraper.momence import MomenceSession

logger = logging.getLogger(__name__)


def upsert_sessions(
    sessions: list[MomenceSession],
    db: Session,
) -> list[MomenceSession]:
    """
    Upsert scraped sessions into the slots table.

    For new slots:      INSERT all fields.
    For existing slots: UPDATE remaining_spots, is_cancelled, last_seen_at only.
                        (Immutable fields like starts_at, price_usd never change.)

    Returns the subset of sessions that are currently available AND are either
    new or newly available since the last scrape.
    """
    if not sessions:
        return []

    momence_ids = [s.momence_id for s in sessions]

    # --- Snapshot previous state BEFORE any writes ---
    existing: dict[int, Slot] = {
        row.momence_id: row
        for row in db.query(Slot).filter(Slot.momence_id.in_(momence_ids)).all()
    }
    previously_available: set[int] = {
        mid for mid, slot in existing.items() if slot.is_available
    }

    # --- Upsert ---
    now = datetime.now(timezone.utc)
    new_count = updated_count = 0

    for session in sessions:
        if session.momence_id in existing:
            slot = existing[session.momence_id]
            slot.remaining_spots = session.remaining_spots
            slot.is_cancelled = session.is_cancelled
            slot.last_seen_at = now
            updated_count += 1
        else:
            slot = Slot(
                momence_id=session.momence_id,
                session_name=session.session_name,
                starts_at=session.starts_at,
                ends_at=session.ends_at,
                duration_minutes=session.duration_minutes,
                location=session.location,
                location_id=session.location_id,
                price_usd=session.price_usd,
                capacity=session.capacity,
                remaining_spots=session.remaining_spots,
                total_spots=session.total_spots,
                is_cancelled=session.is_cancelled,
                booking_url=session.booking_url,
                first_seen_at=now,
                last_seen_at=now,
            )
            db.add(slot)
            new_count += 1

    db.commit()

    logger.info(
        "Slot sync complete: %d inserted, %d updated (%d total)",
        new_count, updated_count, len(sessions),
    )

    # --- Determine newly available ---
    newly_available = [
        s for s in sessions
        if s.is_available and s.momence_id not in previously_available
    ]

    if newly_available:
        logger.info(
            "%d newly available slot(s): %s",
            len(newly_available),
            [s.momence_id for s in newly_available],
        )
    else:
        logger.debug("No newly available slots this cycle")

    return newly_available

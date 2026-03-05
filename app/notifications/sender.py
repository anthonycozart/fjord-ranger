"""
Outbound SMS notifier.

Two public functions:

  notify_user()           — personalized slot match notification. Creates
                            Notification + UserSlotState rows, increments
                            the user's daily counter, and commits.

  send_bulk_release_sms() — bulk release announcement for opted-in users.
                            Does not create Notification rows or increment
                            the daily counter. Announces that slots dropped
                            and highlights up to 2 matching slots.

Both are synchronous — call via asyncio.to_thread from async contexts.
"""

import logging
import os
import random
import re
import string
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.db.models import Message, Notification, User, UserSlotState
from app.db.queries import get_user_slot_state
from app.scraper.momence import MomenceSession

logger = logging.getLogger(__name__)

# Valid US NANP number in E.164 format: +1, area code 2–9, then 9 more digits.
# This rejects 911, 411, 611, short codes, and anything malformed.
_E164_US_RE = re.compile(r"^\+1[2-9]\d{9}$")


def _validate_phone(phone: str) -> None:
    """Raise ValueError if phone is not a valid US E.164 number."""
    if not _E164_US_RE.match(phone):
        raise ValueError(f"Refusing to send SMS — invalid US phone number: {phone!r}")


def _generate_slot_code(db: Session) -> str:
    """
    Generate a unique 6-character alphanumeric slot code.
    Checks the DB to guarantee uniqueness — collision probability
    is negligible (36^6 ≈ 2.2B combinations) but worth guarding.
    """
    chars = string.ascii_uppercase + string.digits
    while True:
        code = "".join(random.choices(chars, k=6))
        if not db.query(Notification).filter_by(slot_code=code).first():
            return code


def _format_sms(session: MomenceSession, slot_code: str) -> str:
    pt = session.starts_at_pt
    day = pt.strftime("%a %b %-d")
    time = pt.strftime("%-I:%M %p")
    spots = f"{session.remaining_spots} spot{'s' if session.remaining_spots != 1 else ''}"

    return (
        f"Fjord Ranger: {session.session_name}\n"
        f"{day} · {time} ({session.duration_minutes} min) · {spots} · ${int(session.price_usd)}\n"
        f"{session.booking_url}\n"
        f"Code {slot_code} · Reply to respond · STOP to unsubscribe."
    )


def notify_user(user: User, session: MomenceSession, db: Session) -> bool:
    """
    Send a slot notification SMS to a user.

    Wraps the full flow in a transaction: DB rows are flushed before
    the Twilio call, then committed only if the send succeeds.

    Returns True if the SMS was sent and the DB was updated, False otherwise.
    """
    from twilio.rest import Client

    _validate_phone(user.phone_number)  # hard stop before any DB or API work
    slot_code = _generate_slot_code(db)
    now = datetime.now(timezone.utc)

    # Create or update the user's slot state
    state = get_user_slot_state(db, user.id, session.momence_id)
    if state is None:
        state = UserSlotState(
            user_id=user.id,
            momence_id=session.momence_id,
            state="notified",
            notified_at=now,
        )
        db.add(state)
    else:
        state.state = "notified"
        state.notified_at = now

    # Stage the notification row (no SID yet — Twilio hasn't confirmed)
    notification = Notification(
        slot_code=slot_code,
        user_id=user.id,
        momence_id=session.momence_id,
    )
    db.add(notification)
    db.flush()  # assign IDs without committing

    message_body = _format_sms(session, slot_code)

    try:
        client = Client(
            os.environ["TWILIO_ACCOUNT_SID"],
            os.environ["TWILIO_AUTH_TOKEN"],
        )
        msg = client.messages.create(
            body=message_body,
            from_=os.environ["TWILIO_PHONE_NUMBER"],
            to=user.phone_number,
        )

        # Twilio confirmed — record SID, log to messages, and commit everything
        notification.twilio_message_sid = msg.sid
        user.daily_notification_count += 1
        db.add(Message(user_id=user.id, role="assistant", body=message_body))
        db.commit()

        logger.info(
            "Notified user %d about slot %d (code=%s, sid=%s)",
            user.id, session.momence_id, slot_code, msg.sid,
        )
        return True

    except Exception as e:
        db.rollback()
        logger.error(
            "Failed to notify user %d about slot %d: %s: %s",
            user.id, session.momence_id, type(e).__name__, e,
        )
        return False


def send_bulk_release_sms(
    user: User,
    matching_slots: list[MomenceSession],
    db: Session,
) -> bool:
    """
    Send a bulk release announcement SMS to an opted-in user.

    Does not create Notification rows (no slot code, no reply disambiguation
    needed for announcements) and does not increment daily_notification_count.

    matching_slots — up to 2 slots from the bulk release that match this
                     user's criteria. May be empty if nothing matched.

    Returns True on success, False on failure.
    """
    from twilio.rest import Client

    _validate_phone(user.phone_number)

    if matching_slots:
        slot_summaries = []
        urls = []
        for slot in matching_slots[:2]:
            pt = slot.starts_at_pt
            slot_summaries.append(
                f"{slot.session_name} {pt.strftime('%a %b %-d')} · {pt.strftime('%-I:%M %p')}"
            )
            urls.append(slot.booking_url)
        matches_text = " and ".join(slot_summaries)
        url_text = " · ".join(urls)
        body = (
            f"Fjord Ranger: New slots just dropped — book fast.\n"
            f"Matches for you: {matches_text}\n"
            f"{url_text}\n"
            f"STOP to unsubscribe."
        )
    else:
        body = (
            "Fjord Ranger: New slots just dropped at Fjord — "
            "get your phone out and book now.\n"
            "STOP to unsubscribe."
        )

    try:
        client = Client(
            os.environ["TWILIO_ACCOUNT_SID"],
            os.environ["TWILIO_AUTH_TOKEN"],
        )
        msg = client.messages.create(
            body=body,
            from_=os.environ["TWILIO_PHONE_NUMBER"],
            to=user.phone_number,
        )
        db.add(Message(user_id=user.id, role="assistant", body=body))
        db.commit()
        logger.info(
            "Bulk release SMS sent to user %d (sid=%s, %d matching slot(s))",
            user.id, msg.sid, len(matching_slots),
        )
        return True

    except Exception as e:
        db.rollback()
        logger.error(
            "Failed to send bulk release SMS to user %d: %s: %s",
            user.id, type(e).__name__, e,
        )
        return False

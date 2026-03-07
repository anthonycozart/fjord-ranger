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
from pathlib import Path

from sqlalchemy.orm import Session

from app.db.models import Message, Notification, User, UserSlotState
from app.db.queries import get_user_slot_state
from app.scraper.momence import MomenceSession

logger = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).parent.parent.parent / "prompts"
_TMPL_SLOT = (PROMPTS_DIR / "sms_slot_notification.txt").read_text(encoding="utf-8")
_TMPL_BULK_MATCHES = (PROMPTS_DIR / "sms_bulk_with_matches.txt").read_text(encoding="utf-8")
_TMPL_BULK_NO_MATCHES = (PROMPTS_DIR / "sms_bulk_no_matches.txt").read_text(encoding="utf-8")
_TMPL_NUDGE = (PROMPTS_DIR / "sms_preferences_nudge.txt").read_text(encoding="utf-8")

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
    spots = f"{session.remaining_spots} spot{'s' if session.remaining_spots != 1 else ''}"
    return _TMPL_SLOT.format(
        session_name=session.session_name,
        day=pt.strftime("%a %b %-d"),
        time=pt.strftime("%-I:%M %p"),
        duration_minutes=session.duration_minutes,
        spots=spots,
        price_usd=int(session.price_usd),
        booking_url=session.booking_url,
        slot_code=slot_code,
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

        # First notification to a criteria-less user — send the preferences nudge
        if user.criteria is None and not user.preferences_nudge_sent:
            _send_preferences_nudge(user, db)

        return True

    except Exception as e:
        db.rollback()
        logger.error(
            "Failed to notify user %d about slot %d: %s: %s",
            user.id, session.momence_id, type(e).__name__, e,
        )
        return False


def _send_preferences_nudge(user: User, db: Session) -> None:
    """
    Send the preferences nudge SMS and mark it as sent. Best-effort —
    failures are logged but never raised so the main notification stands.
    """
    from twilio.rest import Client

    try:
        client = Client(os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"])
        client.messages.create(
            body=_TMPL_NUDGE,
            from_=os.environ["TWILIO_PHONE_NUMBER"],
            to=user.phone_number,
        )
        user.preferences_nudge_sent = True
        db.add(Message(user_id=user.id, role="assistant", body=_TMPL_NUDGE))
        db.commit()
        logger.info("Preferences nudge sent to user %d", user.id)
    except Exception as e:
        db.rollback()
        logger.error(
            "Failed to send preferences nudge to user %d: %s: %s",
            user.id, type(e).__name__, e,
        )


def _format_date_range(slots: list[MomenceSession]) -> str:
    """Format the date range of a list of slots, e.g. 'Apr 1–30' or 'Mar 28 – Apr 5'."""
    dates = sorted({s.starts_at_pt.date() for s in slots})
    if not dates:
        return "new"
    lo, hi = dates[0], dates[-1]
    if lo == hi:
        return lo.strftime("%b %-d")
    if lo.month == hi.month:
        return f"{lo.strftime('%b')} {lo.day}–{hi.day}"
    return f"{lo.strftime('%b %-d')} – {hi.strftime('%b %-d')}"


def send_bulk_release_sms(
    user: User,
    matching_slots: list[MomenceSession],
    all_slots: list[MomenceSession],
    db: Session,
) -> bool:
    """
    Send a bulk release announcement SMS to an opted-in user.

    Does not create Notification rows (no slot code, no reply disambiguation
    needed for announcements) and does not increment daily_notification_count.

    matching_slots — up to 2 slots from the bulk release that match this
                     user's criteria. May be empty if nothing matched.
    all_slots      — all newly available slots in the bulk release, used to
                     compute the date range shown in the message.

    Returns True on success, False on failure.
    """
    from twilio.rest import Client

    _validate_phone(user.phone_number)

    date_range = _format_date_range(all_slots)

    if matching_slots:
        slot_summaries = []
        urls = []
        for slot in matching_slots[:2]:
            pt = slot.starts_at_pt
            slot_summaries.append(
                f"{slot.session_name} {pt.strftime('%a %b %-d')} · {pt.strftime('%-I:%M %p')}"
            )
            urls.append(slot.booking_url)
        body = _TMPL_BULK_MATCHES.format(
            date_range=date_range,
            matches_text=" and ".join(slot_summaries),
            url_text=" · ".join(urls),
        )
    else:
        body = _TMPL_BULK_NO_MATCHES.format(date_range=date_range)

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

        # First notification to a criteria-less user — send the preferences nudge
        if user.criteria is None and not user.preferences_nudge_sent:
            _send_preferences_nudge(user, db)

        return True

    except Exception as e:
        db.rollback()
        logger.error(
            "Failed to send bulk release SMS to user %d: %s: %s",
            user.id, type(e).__name__, e,
        )
        return False

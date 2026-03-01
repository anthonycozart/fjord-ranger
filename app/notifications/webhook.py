"""
Inbound SMS webhook handler (POST /webhook/sms).

Twilio forwards every inbound SMS to this endpoint. The handler:
  1. Validates the Twilio request signature (rejects spoofed requests).
  2. Routes by message body:
       NO-<code>  → mark the slot rejected for this user
       STOP       → opt the user out in our DB (Twilio also handles compliance)
       RANGER     → register the sender as a pending user (onboarding stub)
       (anything) → reply with help text
  3. Returns a TwiML <Response> so Twilio can deliver a reply if needed.

All DB work is offloaded to a thread (asyncio.to_thread) so the async event
loop is never blocked by synchronous SQLAlchemy calls.
"""

import asyncio
import logging
import os
import re

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from app.db.database import get_session_factory
from app.db.models import Notification, User, UserSlotState
from app.notifications.sender import _validate_phone

logger = logging.getLogger(__name__)

router = APIRouter()

SIGNUP_KEYWORD = os.environ.get("SIGNUP_KEYWORD", "RANGER").upper()
_NO_CODE_RE = re.compile(r"^NO-([A-Z0-9]{6})$", re.IGNORECASE)


# ---------------------------------------------------------------------------
# TwiML helpers
# ---------------------------------------------------------------------------

def _twiml(message: str) -> Response:
    """Return a TwiML MessagingResponse that sends one reply SMS."""
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f"<Response><Message>{message}</Message></Response>"
    )
    return Response(content=xml, media_type="application/xml")


def _twiml_empty() -> Response:
    """Return an empty TwiML response — no reply SMS sent.

    Used for STOP: Twilio delivers its own compliance message; we just
    need to acknowledge receipt without double-replying.
    """
    return Response(
        content='<?xml version="1.0" encoding="UTF-8"?><Response/>',
        media_type="application/xml",
    )


# ---------------------------------------------------------------------------
# Webhook entry point
# ---------------------------------------------------------------------------

@router.post("/webhook/sms")
async def sms_webhook(request: Request) -> Response:
    """Handle an inbound SMS forwarded by Twilio."""
    from twilio.request_validator import RequestValidator

    # Parse the form-encoded POST body Twilio sends
    form = await request.form()
    from_number = str(form.get("From", "")).strip()
    body = str(form.get("Body", "")).strip()

    # Reject anything that doesn't have a valid Twilio signature
    validator = RequestValidator(os.environ["TWILIO_AUTH_TOKEN"])
    signature = request.headers.get("X-Twilio-Signature", "")
    if not validator.validate(str(request.url), dict(form), signature):
        logger.warning("Invalid Twilio signature from %s", from_number)
        raise HTTPException(status_code=403, detail="Forbidden")

    logger.info("Inbound SMS from %s: %r", from_number, body)

    return await asyncio.to_thread(_handle_sms, from_number, body)


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------

def _handle_sms(from_number: str, body: str) -> Response:
    """Route the inbound message and return a TwiML response. Runs in a thread."""
    upper = body.upper()

    match = _NO_CODE_RE.match(upper)
    if match:
        return _handle_rejection(from_number, match.group(1).upper(), body)

    if upper == "STOP":
        return _handle_stop(from_number)

    if upper == SIGNUP_KEYWORD:
        return _handle_signup(from_number)

    return _twiml(
        "Fjord Ranger: Reply NO-<CODE> (e.g. NO-A3F2K1) to skip a notified slot. "
        "Reply STOP to unsubscribe."
    )


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

def _handle_rejection(from_number: str, slot_code: str, raw_body: str) -> Response:
    """Mark a notified slot as rejected for this user."""
    db = get_session_factory()()
    try:
        notification = (
            db.query(Notification).filter_by(slot_code=slot_code).first()
        )
        if notification is None:
            logger.info("NO-%s: code not found (from %s)", slot_code, from_number)
            return _twiml(
                "That code wasn't found. Check your original message and try again."
            )

        # Security: verify the reply came from the same number the SMS was sent to
        user = db.query(User).filter_by(id=notification.user_id).first()
        if user is None or user.phone_number != from_number:
            logger.warning(
                "NO-%s: sender %s doesn't match notification owner — rejecting",
                slot_code,
                from_number,
            )
            return _twiml(
                "That code wasn't found. Check your original message and try again."
            )

        # Transition the slot state to rejected
        state = (
            db.query(UserSlotState)
            .filter_by(user_id=notification.user_id, momence_id=notification.momence_id)
            .first()
        )
        if state is not None:
            state.state = "rejected"
            state.feedback_raw = raw_body
        else:
            # Shouldn't happen if sender.py is working correctly, but be defensive
            logger.warning(
                "NO-%s: no UserSlotState row for user %d / slot %d — creating one",
                slot_code,
                notification.user_id,
                notification.momence_id,
            )
            db.add(
                UserSlotState(
                    user_id=notification.user_id,
                    momence_id=notification.momence_id,
                    state="rejected",
                    feedback_raw=raw_body,
                )
            )

        db.commit()
        logger.info(
            "Slot %d marked rejected for user %d (code=%s)",
            notification.momence_id,
            notification.user_id,
            slot_code,
        )
        return _twiml("Got it — we'll skip that slot.")

    except Exception as e:
        db.rollback()
        logger.error(
            "Error processing rejection code %s: %s: %s", slot_code, type(e).__name__, e
        )
        return _twiml("Something went wrong. Please try again.")
    finally:
        db.close()


def _handle_stop(from_number: str) -> Response:
    """Update our DB when a user opts out via STOP.

    Twilio intercepts STOP at the carrier level and sends its own compliance
    reply, so we return an empty TwiML response to avoid double-messaging.
    """
    db = get_session_factory()()
    try:
        user = db.query(User).filter_by(phone_number=from_number).first()
        if user is not None and user.status != "opted_out":
            user.status = "opted_out"
            db.commit()
            logger.info("User %d opted out via STOP", user.id)
        else:
            logger.debug("STOP from unregistered number %s — no action needed", from_number)
        return _twiml_empty()
    except Exception as e:
        db.rollback()
        logger.error("Error handling STOP from %s: %s: %s", from_number, type(e).__name__, e)
        return _twiml_empty()
    finally:
        db.close()


def _handle_signup(from_number: str) -> Response:
    """Register a new user as pending.

    Full criteria-collection onboarding is a future milestone (M8). For now,
    the user lands in 'pending' status and the admin sets their criteria via
    the admin CLI. The reply message sets this expectation.
    """
    try:
        _validate_phone(from_number)
    except ValueError:
        logger.warning("Signup attempt from invalid number %r — ignoring", from_number)
        return _twiml("Sorry, we couldn't register that number.")

    db = get_session_factory()()
    try:
        existing = db.query(User).filter_by(phone_number=from_number).first()
        if existing is not None:
            if existing.status == "opted_out":
                return _twiml(
                    "You're currently unsubscribed from Fjord Ranger. "
                    "Contact the admin to re-join."
                )
            return _twiml(
                "You're already registered with Fjord Ranger! "
                "You'll get a text when a slot matches your preferences."
            )

        new_user = User(phone_number=from_number, status="pending")
        db.add(new_user)
        db.commit()
        logger.info(
            "New user registered from %s (id=%d)", from_number, new_user.id
        )

        return _twiml(
            "Welcome to Fjord Ranger! You're on the list. "
            "The admin will set up your slot preferences — "
            "we'll text you when a match opens up. Reply STOP to unsubscribe."
        )

    except Exception as e:
        db.rollback()
        logger.error("Error handling signup from %s: %s: %s", from_number, type(e).__name__, e)
        return _twiml("Something went wrong. Please try again.")
    finally:
        db.close()

"""
Inbound SMS webhook handler (POST /webhook/sms).

Twilio forwards every inbound SMS to this endpoint. The handler:
  1. Validates the Twilio request signature (rejects spoofed requests).
  2. Routes by message body:
       STOP            → opt the user out in our DB (Twilio also handles compliance)
       SIGNUP_KEYWORD  → register the sender as a pending user (onboarding stub)
       (anything else) → conversation agent (Claude Sonnet + tool use)
  3. Returns a TwiML <Response> so Twilio can deliver a reply.

All natural-language replies — including slot rejections — are handled by the
conversation agent, which interprets intent and takes the appropriate action.
"""

import asyncio
import logging
import os

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from app.db.database import get_session_factory
from app.db.models import Message, User
from app.notifications.sender import _validate_phone

logger = logging.getLogger(__name__)

router = APIRouter()

SIGNUP_KEYWORD = os.environ.get("SIGNUP_KEYWORD", "RANGER").upper()


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


def _twiml_multi(*messages: str) -> Response:
    """Return a TwiML MessagingResponse that sends multiple SMS segments."""
    body = "".join(f"<Message>{m}</Message>" for m in messages)
    xml = f'<?xml version="1.0" encoding="UTF-8"?><Response>{body}</Response>'
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

    form = await request.form()
    from_number = str(form.get("From", "")).strip()
    body = str(form.get("Body", "")).strip()

    # Reject anything without a valid Twilio signature
    validator = RequestValidator(os.environ["TWILIO_AUTH_TOKEN"])
    signature = request.headers.get("X-Twilio-Signature", "")
    if not validator.validate(str(request.url), dict(form), signature):
        logger.warning("Invalid Twilio signature from %s", from_number)
        raise HTTPException(status_code=403, detail="Forbidden")

    logger.info("Inbound SMS from %s: %r", from_number, body)

    upper = body.upper()

    if upper == "STOP":
        return await asyncio.to_thread(_handle_stop, from_number)

    if upper == SIGNUP_KEYWORD:
        return await asyncio.to_thread(_handle_signup, from_number)

    # All other messages go to the conversation agent
    return await _handle_conversation(from_number, body)


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

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
    """Register a new user as active.

    The keyword is the invite — no admin approval needed. Users start
    active with no criteria, meaning they'll be notified of all slots
    until they share preferences via a reply.
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

        new_user = User(phone_number=from_number, status="active")
        db.add(new_user)
        db.commit()
        logger.info("New user registered from %s (id=%d)", from_number, new_user.id)

        return _twiml_multi(
            "Welcome to Fjord Ranger! You've opted in to sauna slot alerts for Fjord (Sausalito). "
            "Msg freq varies. Msg & data rates may apply. "
            "Reply STOP to unsubscribe, HELP for help. "
            "Terms & Privacy: https://anthonycozart.github.io/fjord-ranger/",
            "Please share your preferences so we know when to text you about new availability. "
            "You can be as general or specific as you like, just respond in natural language "
            "(e.g., \"anytime\" or \"I prefer private sessions on Fridays, ideally after 4pm.\").",
        )

    except Exception as e:
        db.rollback()
        logger.error("Error handling signup from %s: %s: %s", from_number, type(e).__name__, e)
        return _twiml("Something went wrong. Please try again.")
    finally:
        db.close()


async def _handle_conversation(from_number: str, body: str) -> Response:
    """Route a reply to the conversation agent.

    Looks up the user, stores the inbound message, runs the agent,
    stores the outbound reply, and returns a TwiML response.
    """
    from app.agent.conversation import handle_reply

    db = get_session_factory()()
    try:
        user = db.query(User).filter_by(phone_number=from_number).first()
        if user is None:
            return _twiml(
                f"Text {SIGNUP_KEYWORD} to sign up for Fjord Ranger."
            )

        # Store the inbound message in the conversation thread
        db.add(Message(user_id=user.id, role="user", body=body))
        db.flush()

        # Run the conversation agent (async — awaits Claude API)
        reply = await handle_reply(user, body, db)

        # Store the agent's reply and commit everything atomically
        db.add(Message(user_id=user.id, role="assistant", body=reply))
        db.commit()

        return _twiml(reply)

    except Exception as e:
        db.rollback()
        logger.error(
            "Conversation error for %s: %s: %s", from_number, type(e).__name__, e
        )
        return _twiml("Something went wrong. Please try again.")
    finally:
        db.close()

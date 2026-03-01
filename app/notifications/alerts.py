"""
Admin SMS alerts for operational issues (scraper failures, dead-man's switch).

This is separate from user notifications — no daily caps, no slot codes,
no criteria matching. Just fires a message directly to the admin phone number.

Required env vars:
  TWILIO_ACCOUNT_SID
  TWILIO_AUTH_TOKEN
  TWILIO_PHONE_NUMBER   — your Twilio number (from_)
  ADMIN_PHONE_NUMBER    — your personal number (to)
"""

import asyncio
import logging
import os

logger = logging.getLogger(__name__)


def _send_sms_sync(message: str) -> None:
    """Synchronous Twilio send — runs in a thread to avoid blocking the event loop."""
    from twilio.rest import Client  # lazy import — not installed in dev until needed

    client = Client(
        os.environ["TWILIO_ACCOUNT_SID"],
        os.environ["TWILIO_AUTH_TOKEN"],
    )
    client.messages.create(
        body=f"[Fjord Ranger] {message}",
        from_=os.environ["TWILIO_PHONE_NUMBER"],
        to=os.environ["ADMIN_PHONE_NUMBER"],
    )


async def send_admin_alert(message: str) -> None:
    """
    Send an SMS alert to the admin. Non-blocking, never raises.

    Failures are logged but swallowed — alerting failure shouldn't
    crash or mask the original error.
    """
    try:
        await asyncio.to_thread(_send_sms_sync, message)
        logger.info("Admin alert sent: %.60s", message)
    except Exception as e:
        logger.error("Failed to send admin alert (%s): %s", type(e).__name__, e)

"""
Scrape job: fetches Momence sessions on a schedule and processes them.

Failure handling:
  - Single failures are logged and skipped (transient network issues happen)
  - After ALERT_AFTER_N_FAILURES consecutive failures, an admin SMS is sent
  - Alert is sent exactly once per failure streak, not on every failure
  - Streak resets to 0 on the next successful scrape

Dead-man's switch:
  - check_dead_mans_switch() runs every 2 hours via APScheduler
  - Alerts if no successful scrape has completed in DEAD_MANS_WINDOW_HOURS
  - Catches silent failures where the scheduler stops running without crashing
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import httpx

from app.agent.analyzer import matches_criteria
from app.db.database import get_session_factory
from app.db.models import User
from app.db.queries import get_notifiable_users, get_user_slot_state
from app.db.sync import upsert_sessions
from app.notifications.alerts import send_admin_alert
from app.notifications.sender import notify_user
from app.scraper.momence import MomenceSession, fetch_all_sessions

logger = logging.getLogger(__name__)

# Alert after this many consecutive failures (not on every individual failure)
ALERT_AFTER_N_FAILURES = 3

# Alert if no successful scrape in this many hours
DEAD_MANS_WINDOW_HOURS = 2

# In-memory state — persists for the lifetime of the process
_consecutive_failures: int = 0
_last_success_at: datetime | None = None


async def run_scrape_cycle() -> None:
    """
    Main scrape job. Called by APScheduler every 5 minutes, 6am–10pm PT.

    On success: resets failure counter, updates last_success timestamp.
    On failure: increments counter, sends admin alert at threshold.
    """
    global _consecutive_failures, _last_success_at

    logger.info("Scrape cycle starting")

    try:
        sessions = await fetch_all_sessions()

        # Success — reset failure tracking
        _consecutive_failures = 0
        _last_success_at = datetime.now(timezone.utc)

        # Upsert into DB, get back only newly available slots
        db = get_session_factory()()
        try:
            newly_available = await asyncio.to_thread(upsert_sessions, sessions, db)
        finally:
            db.close()

        logger.info(
            "Scrape complete: %d total, %d newly available",
            len(sessions), len(newly_available),
        )

        if newly_available:
            await _analyze_and_queue(newly_available)

    except httpx.HTTPStatusError as e:
        await _handle_failure(
            f"Momence API returned HTTP {e.response.status_code}",
            e,
        )
    except httpx.TimeoutException as e:
        await _handle_failure("Momence API request timed out", e)
    except httpx.RequestError as e:
        await _handle_failure(f"Network error reaching Momence API", e)
    except Exception as e:
        await _handle_failure(f"Unexpected error in scrape cycle", e)


async def check_dead_mans_switch() -> None:
    """
    Dead-man's switch: alerts if no successful scrape recently.
    Scheduled to run every 2 hours by APScheduler.
    """
    if _last_success_at is None:
        # Service just started — give it one cycle before worrying
        logger.debug("Dead-man's switch: no scrape yet (service just started)")
        return

    age = datetime.now(timezone.utc) - _last_success_at
    hours = age.total_seconds() / 3600

    if age > timedelta(hours=DEAD_MANS_WINDOW_HOURS):
        logger.warning("Dead-man's switch triggered: last success %.1fh ago", hours)
        await send_admin_alert(
            f"No successful scrape in {hours:.1f}h. "
            f"Last success: {_last_success_at.strftime('%Y-%m-%d %H:%M UTC')}. "
            f"Check Railway logs."
        )
    else:
        logger.debug("Dead-man's switch OK: last success %.1fh ago", hours)


async def _analyze_and_queue(newly_available: list[MomenceSession]) -> None:
    """
    For each newly available slot, check it against every notifiable user's
    criteria via Claude. On match, sends an SMS notification via the notifier.

    Users are fetched once per cycle (not once per slot) to avoid redundant
    DB round-trips. Analyzer calls run concurrently, bounded by a semaphore
    to avoid hitting Claude rate limits if many slots open at once.
    """
    db = get_session_factory()()
    try:
        users = await asyncio.to_thread(get_notifiable_users, db)
    finally:
        db.close()

    if not users:
        logger.debug("No notifiable users — skipping analysis")
        return

    logger.info(
        "Analyzing %d newly available slot(s) against %d user(s)",
        len(newly_available), len(users),
    )

    # Limit concurrent Claude calls (generous for Haiku, but a good guardrail)
    semaphore = asyncio.Semaphore(10)

    async def evaluate(session: MomenceSession, user) -> None:
        async with semaphore:
            db = get_session_factory()()
            try:
                # Skip if this user already has a state for this slot
                existing = await asyncio.to_thread(
                    get_user_slot_state, db, user.id, session.momence_id
                )
                if existing and existing.state in ("notified", "rejected", "expired"):
                    logger.debug(
                        "Slot %d already %s for user %d — skipping",
                        session.momence_id, existing.state, user.id,
                    )
                    return
            finally:
                db.close()

            try:
                result = await matches_criteria(session, user.criteria.criteria)
            except Exception as e:
                logger.error(
                    "Analyzer error for slot %d / user %d: %s",
                    session.momence_id, user.id, e,
                )
                return

            if result.matches:
                logger.info(
                    "MATCH — slot %d (%s) for user %d: %s",
                    session.momence_id, session.describe(), user.id, result.reasoning,
                )
                captured_user_id = user.id  # int — safe to capture across thread boundary

                def _notify() -> None:
                    notify_db = get_session_factory()()
                    try:
                        # Re-fetch user so ORM mutations (daily_count++) can be committed
                        fresh_user = (
                            notify_db.query(User).filter_by(id=captured_user_id).first()
                        )
                        if fresh_user is None or not fresh_user.is_notifiable:
                            # Another concurrent task may have already sent today's cap
                            logger.debug(
                                "User %d no longer notifiable — skipping notify",
                                captured_user_id,
                            )
                            return
                        notify_user(fresh_user, session, notify_db)
                    finally:
                        notify_db.close()

                await asyncio.to_thread(_notify)
            else:
                logger.debug(
                    "No match — slot %d for user %d: %s",
                    session.momence_id, user.id, result.reasoning,
                )

    tasks = [
        evaluate(session, user)
        for session in newly_available
        for user in users
    ]
    await asyncio.gather(*tasks)


async def _handle_failure(reason: str, exc: Exception) -> None:
    """Increment failure counter and alert at threshold."""
    global _consecutive_failures

    _consecutive_failures += 1
    logger.error(
        "Scrape failed (%d consecutive): %s — %s: %s",
        _consecutive_failures,
        reason,
        type(exc).__name__,
        exc,
    )

    if _consecutive_failures == ALERT_AFTER_N_FAILURES:
        # Alert exactly once when we hit the threshold, not on every subsequent failure
        await send_admin_alert(
            f"Scraper has failed {_consecutive_failures} times in a row.\n"
            f"Reason: {reason}\n"
            f"Error: {type(exc).__name__}: {str(exc)[:120]}\n"
            f"Check Railway logs."
        )
    elif _consecutive_failures > ALERT_AFTER_N_FAILURES:
        logger.warning(
            "Failure streak continues (%d failures) — alert already sent, staying quiet",
            _consecutive_failures,
        )

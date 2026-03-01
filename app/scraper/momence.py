"""
Momence API scraper for Fjord slot availability.

Momence exposes a public REST API — no browser automation needed.
Each slot has a stable integer ID (momence_id) used as its primary key
throughout the system, and directly in the booking URL:
  https://momence.com/s/{momence_id}

Key endpoints:
  Sessions (paginated):
    GET https://readonly-api.momence.com/host-plugins/host/46052/host-schedule/sessions
        ?sessionTypes[]=...&fromDate=<ISO UTC>&pageSize=100&page=0

  Available dates (for quick diffing — tells us which dates have sessions):
    GET https://readonly-api.momence.com/host-plugins/host/46052/host-schedule/dates
        ?sessionTypes[]=...&timeZone=America/Los_Angeles
"""

import logging
import random
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

# Fjord's Momence host ID (discovered via network interception)
HOST_ID = 46052
BASE_URL = "https://readonly-api.momence.com/host-plugins/host"
PT = ZoneInfo("America/Los_Angeles")

# All session types Fjord uses
SESSION_TYPES = [
    "course-class",
    "fitness",
    "retreat",
    "special-event",
    "special-event-new",
]

# Rotate user agents to look like a real browser
_USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
]


@dataclass
class MomenceSession:
    """A single bookable slot returned by the Momence API."""

    momence_id: int
    session_name: str
    starts_at: datetime        # UTC
    ends_at: datetime          # UTC
    duration_minutes: int
    location: str
    location_id: int
    price_usd: float
    capacity: int              # booking slots (1 for private, 8 for shared)
    remaining_spots: int
    total_spots: int
    is_cancelled: bool
    allow_waitlist: bool
    waitlist_full: bool
    booking_url: str

    @property
    def is_available(self) -> bool:
        """True if the slot can actually be booked right now."""
        return not self.is_cancelled and self.remaining_spots > 0

    @property
    def starts_at_pt(self) -> datetime:
        """Start time in Pacific time (for display and criteria matching)."""
        return self.starts_at.astimezone(PT)

    def describe(self) -> str:
        """Human-readable one-liner for logs and SMS."""
        pt = self.starts_at_pt
        day = pt.strftime("%a %b %-d")
        time = pt.strftime("%-I:%M %p")
        status = f"{self.remaining_spots}/{self.total_spots} spots" if self.is_available else "Full"
        return f"{self.session_name} — {day} at {time} ({self.duration_minutes} min) [{status}] ${self.price_usd:.0f}"


def _build_session_params(from_date: str, page: int, page_size: int = 100) -> dict:
    """Build query params for the sessions endpoint."""
    params = [(f"sessionTypes[]", t) for t in SESSION_TYPES]
    params += [
        ("fromDate", from_date),
        ("pageSize", str(page_size)),
        ("page", str(page)),
    ]
    return params


async def fetch_all_sessions(
    from_dt: datetime | None = None,
    page_size: int = 100,
) -> list[MomenceSession]:
    """
    Fetch all upcoming sessions from the Momence API.

    Args:
        from_dt: Start of the date range (UTC). Defaults to now.
        page_size: Slots per API page (max appears to be 100).

    Returns:
        List of MomenceSession objects, ordered by startsAt ascending.

    Raises:
        httpx.HTTPStatusError: On non-2xx API response.
        httpx.TimeoutException: If the request times out.
    """
    if from_dt is None:
        from_dt = datetime.now(timezone.utc)

    from_date_str = from_dt.isoformat()
    sessions: list[MomenceSession] = []
    page = 0

    headers = {
        "User-Agent": random.choice(_USER_AGENTS),
        "Accept": "application/json",
        "Referer": "https://momence.com/",
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        while True:
            url = f"{BASE_URL}/{HOST_ID}/host-schedule/sessions"
            params = _build_session_params(from_date_str, page, page_size)

            logger.debug("Fetching sessions page=%d from=%s", page, from_date_str)
            resp = await client.get(url, params=params, headers=headers)
            resp.raise_for_status()

            data = resp.json()
            payload: list[dict] = data.get("payload", [])

            if not payload:
                break

            for raw in payload:
                try:
                    sessions.append(_parse_session(raw))
                except (KeyError, ValueError) as e:
                    logger.warning("Failed to parse session %s: %s", raw.get("id"), e)

            total_count: int = data.get("pagination", {}).get("totalCount", 0)
            fetched_so_far = (page + 1) * page_size

            logger.debug(
                "Fetched %d sessions (page %d, total %d)",
                len(payload), page, total_count,
            )

            if fetched_so_far >= total_count:
                break

            page += 1

    logger.info("Fetched %d total sessions from Momence", len(sessions))
    return sessions


async def fetch_available_sessions(from_dt: datetime | None = None) -> list[MomenceSession]:
    """Convenience wrapper: returns only sessions where remaining_spots > 0."""
    all_sessions = await fetch_all_sessions(from_dt=from_dt)
    available = [s for s in all_sessions if s.is_available]
    logger.info("%d/%d sessions have availability", len(available), len(all_sessions))
    return available


def _parse_session(raw: dict) -> MomenceSession:
    spots = raw.get("remainingSpots") or {}
    capacity = raw.get("capacity") or 0
    return MomenceSession(
        momence_id=raw["id"],
        session_name=raw["sessionName"],
        starts_at=datetime.fromisoformat(raw["startsAt"].replace("Z", "+00:00")),
        ends_at=datetime.fromisoformat(raw["endsAt"].replace("Z", "+00:00")),
        duration_minutes=raw["durationMinutes"],
        location=raw["location"].strip(),
        location_id=raw["locationId"],
        price_usd=float(raw.get("fixedTicketPrice") or 0),
        capacity=capacity,
        remaining_spots=spots.get("remaining", 0),
        total_spots=spots.get("total", capacity),
        is_cancelled=raw["isCancelled"],
        allow_waitlist=raw.get("allowWaitlist", False),
        waitlist_full=raw.get("waitlistFull", True),
        booking_url=raw["link"],
    )

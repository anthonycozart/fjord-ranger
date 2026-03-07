"""
Slot criteria analyzer.

Loads prompts from prompts/ so they can be tuned without touching
application code. Prompts are re-read from disk on each call, meaning
changes take effect immediately without a service restart.
"""

import json
import logging
from dataclasses import dataclass
from pathlib import Path

import anthropic

from app.scraper.momence import MomenceSession

logger = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).parent.parent.parent / "prompts"
MODEL = "claude-haiku-4-5-20251001"

# Shared async client — initialised once per process
_client: anthropic.AsyncAnthropic | None = None


def _get_client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic()  # reads ANTHROPIC_API_KEY from env
    return _client


def _load_prompt(filename: str) -> str:
    return (PROMPTS_DIR / filename).read_text(encoding="utf-8")


@dataclass
class AnalysisResult:
    matches: bool
    reasoning: str


async def matches_criteria(
    session: MomenceSession,
    criteria: dict | None,
) -> AnalysisResult:
    """
    Ask Claude Haiku whether a slot matches a user's criteria.

    If criteria is None or empty, the user has no preferences set and we
    treat every slot as a match — they'll receive a nudge after the first
    notification prompting them to share preferences.

    Args:
        session:  The available MomenceSession to evaluate.
        criteria: The user's criteria dict, or None if not set.

    Returns:
        AnalysisResult(matches, reasoning).

    Raises:
        anthropic.APIError: On API failure (let caller decide whether to retry).
        ValueError: If Claude returns malformed JSON.
    """
    if not criteria:
        return AnalysisResult(matches=True, reasoning="No criteria set — notifying for all slots.")

    system_prompt = _load_prompt("analyzer_system.txt")
    user_prompt = _load_prompt("slot_match.txt").format(
        session_name=session.session_name,
        day_of_week=session.starts_at_pt.strftime("%A"),
        date=session.starts_at_pt.strftime("%B %-d, %Y"),
        start_time=session.starts_at_pt.strftime("%-I:%M %p"),
        duration_minutes=session.duration_minutes,
        remaining_spots=session.remaining_spots,
        total_spots=session.total_spots,
        price_usd=int(session.price_usd),
        location=session.location,
        criteria_json=json.dumps(criteria, indent=2),
    )

    response = await _get_client().messages.create(
        model=MODEL,
        max_tokens=256,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )

    raw = response.content[0].text.strip()
    # Strip markdown code fences if the model wraps its output despite instructions
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    logger.debug("Analyzer raw response for slot %d: %s", session.momence_id, raw)

    try:
        result = json.loads(raw)
        return AnalysisResult(
            matches=bool(result["matches"]),
            reasoning=str(result["reasoning"]),
        )
    except (json.JSONDecodeError, KeyError) as e:
        raise ValueError(
            f"Analyzer returned invalid JSON for slot {session.momence_id}: {raw!r}"
        ) from e

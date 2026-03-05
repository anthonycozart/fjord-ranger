"""
Conversation agent.

Handles inbound user replies via Claude Sonnet with extended thinking and
tool use. The agent can observe and update state but never takes external
actions on the user's behalf (no booking).

Extended thinking is enabled so the LLM's reasoning is captured in full
in the agent_turns log — use this to study and tune the agent's decisions.

Tools available to the agent:
  mark_slot_rejected    — mark a notified slot as rejected for this user
  get_available_slots   — fetch slots currently open in the DB
  get_slot_details      — fetch details for a specific slot
  update_user_criteria  — update the user's preferences based on conversation
  pause_notifications   — pause notifications until the user asks to resume

Call handle_reply() from the webhook. It is async (awaits the Claude API)
and manages its own DB flushing; the caller is responsible for commit/rollback.
"""

import json
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import anthropic

from app.db.models import (
    AgentTurn,
    Message,
    Notification,
    Slot,
    User,
    UserCriteria,
    UserSlotState,
)
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).parent.parent.parent / "prompts"
MODEL = "claude-sonnet-4-6"
THINKING_BUDGET = 3000   # tokens reserved for extended thinking per round
MAX_ROUNDS = 4           # hard cap on agentic rounds to prevent runaway loops

# Shared async client
_client: anthropic.AsyncAnthropic | None = None


def _get_client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic()
    return _client


# ---------------------------------------------------------------------------
# Tool definitions (passed to the Claude API)
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "name": "mark_slot_rejected",
        "description": (
            "Mark a specific slot as rejected for this user. "
            "Call this when the user indicates they cannot make a slot, "
            "are not interested, or want to skip it."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "momence_id": {
                    "type": "integer",
                    "description": "The Momence ID of the slot to reject.",
                }
            },
            "required": ["momence_id"],
        },
    },
    {
        "name": "get_available_slots",
        "description": (
            "Get slots that are currently available in the database. "
            "Call this when the user asks about other options, availability, "
            "or 'anything else'. Returns up to 5 upcoming slots."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "get_slot_details",
        "description": (
            "Get full details for a specific slot by its Momence ID. "
            "Call this when the user asks a specific question about a slot."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "momence_id": {
                    "type": "integer",
                    "description": "The Momence ID of the slot.",
                }
            },
            "required": ["momence_id"],
        },
    },
    {
        "name": "update_user_criteria",
        "description": (
            "Update the user's slot preferences based on what they've told you. "
            "Call this when the user expresses a change in when or what type of "
            "session they want. Provide only the fields that should change — "
            "other fields are preserved."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "changes": {
                    "type": "object",
                    "description": (
                        "Criteria fields to update. Valid keys: "
                        "preferred_days (array of day names), "
                        "time_window ({earliest: 'HH:MM', latest: 'HH:MM'}), "
                        "session_names (array: 'Private Session' or 'Shared Session (90 Min)'), "
                        "min_spots (integer), "
                        "bulk_release_alerts (boolean: true to receive blasts when many slots drop at once, false to opt out)."
                    ),
                }
            },
            "required": ["changes"],
        },
    },
    {
        "name": "pause_notifications",
        "description": (
            "Pause notifications for this user. Call this when the user says "
            "they're traveling, busy, or don't want to be notified for a while. "
            "They can resume by replying to any future message."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
]


# ---------------------------------------------------------------------------
# Tool execution
# ---------------------------------------------------------------------------

def _execute_tool(name: str, inputs: dict, user: User, db: Session) -> dict:
    """Dispatch a tool call and return a result dict."""
    try:
        if name == "mark_slot_rejected":
            return _tool_mark_slot_rejected(inputs["momence_id"], user, db)
        if name == "get_available_slots":
            return _tool_get_available_slots(db)
        if name == "get_slot_details":
            return _tool_get_slot_details(inputs["momence_id"], db)
        if name == "update_user_criteria":
            return _tool_update_user_criteria(inputs["changes"], user, db)
        if name == "pause_notifications":
            return _tool_pause_notifications(user, db)
        return {"error": f"Unknown tool: {name}"}
    except Exception as e:
        logger.error("Tool %s failed: %s: %s", name, type(e).__name__, e)
        return {"error": str(e)}


def _tool_mark_slot_rejected(momence_id: int, user: User, db: Session) -> dict:
    state = (
        db.query(UserSlotState)
        .filter_by(user_id=user.id, momence_id=momence_id)
        .first()
    )
    if state is None:
        return {"success": False, "error": "No slot state found for that ID."}
    state.state = "rejected"
    db.flush()
    logger.info("Agent marked slot %d rejected for user %d", momence_id, user.id)
    return {"success": True, "momence_id": momence_id}


def _tool_get_available_slots(db: Session) -> dict:
    now = datetime.now(timezone.utc)
    slots = (
        db.query(Slot)
        .filter(
            Slot.remaining_spots > 0,
            Slot.is_cancelled == False,
            Slot.starts_at > now,
        )
        .order_by(Slot.starts_at)
        .limit(5)
        .all()
    )
    return {
        "slots": [
            {
                "momence_id": s.momence_id,
                "session_name": s.session_name,
                "starts_at_pt": s.starts_at.isoformat(),
                "duration_minutes": s.duration_minutes,
                "remaining_spots": s.remaining_spots,
                "price_usd": float(s.price_usd),
                "booking_url": s.booking_url,
            }
            for s in slots
        ]
    }


def _tool_get_slot_details(momence_id: int, db: Session) -> dict:
    slot = db.query(Slot).filter_by(momence_id=momence_id).first()
    if slot is None:
        return {"error": "Slot not found."}
    now = datetime.now(timezone.utc)
    return {
        "momence_id": slot.momence_id,
        "session_name": slot.session_name,
        "starts_at_pt": slot.starts_at.isoformat(),
        "duration_minutes": slot.duration_minutes,
        "remaining_spots": slot.remaining_spots,
        "price_usd": float(slot.price_usd),
        "location": slot.location,
        "booking_url": slot.booking_url,
        "is_available": slot.is_available,
        "already_passed": slot.starts_at <= now,
    }


def _tool_update_user_criteria(changes: dict, user: User, db: Session) -> dict:
    criteria_row = db.query(UserCriteria).filter_by(user_id=user.id).first()
    if criteria_row is None:
        criteria_row = UserCriteria(user_id=user.id, criteria=changes)
        db.add(criteria_row)
    else:
        updated = {**(criteria_row.criteria or {}), **changes}
        criteria_row.criteria = updated
    db.flush()
    logger.info("Agent updated criteria for user %d: %s", user.id, changes)
    return {"success": True, "updated_criteria": criteria_row.criteria}


def _tool_pause_notifications(user: User, db: Session) -> dict:
    user.status = "paused"
    db.flush()
    logger.info("Agent paused notifications for user %d", user.id)
    return {"success": True, "message": "Notifications paused. Reply any time to resume."}


# ---------------------------------------------------------------------------
# Context building
# ---------------------------------------------------------------------------

def _estimate_tokens(text: str) -> int:
    """Rough estimate: 1 token ≈ 4 characters for English SMS text."""
    return max(1, len(text) // 4)


def _build_user_message(user_message: str, user: User, db: Session) -> str:
    """
    Construct the full context block passed as the user turn to the LLM.
    Includes: the raw message, pending notifications, user criteria, and
    recent conversation history (last 14 days, capped at 1000 tokens).
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=14)

    # Pending notifications: slots we've told this user about that haven't
    # been rejected, expired, or booked yet
    pending = (
        db.query(Notification, Slot)
        .join(Slot, Notification.momence_id == Slot.momence_id)
        .join(
            UserSlotState,
            (UserSlotState.user_id == Notification.user_id)
            & (UserSlotState.momence_id == Notification.momence_id),
        )
        .filter(
            Notification.user_id == user.id,
            UserSlotState.state == "notified",
            Slot.starts_at > now,
        )
        .order_by(Notification.sent_at.desc())
        .limit(5)
        .all()
    )

    pending_lines = []
    for notif, slot in pending:
        pt = slot.starts_at  # already stored as UTC; display as-is for LLM
        pending_lines.append(
            f"  - [Code {notif.slot_code}] {slot.session_name} · "
            f"{pt.strftime('%a %b %-d · %-I:%M %p')} · "
            f"{slot.remaining_spots} spot(s) · ${int(slot.price_usd)} · "
            f"{slot.booking_url}"
        )

    # Conversation history: last 14 days, trimmed to 1000 tokens
    history_rows = (
        db.query(Message)
        .filter(Message.user_id == user.id, Message.created_at >= cutoff)
        .order_by(Message.created_at.desc())
        .all()
    )
    token_budget = 1000
    kept, used = [], 0
    for row in history_rows:  # newest first — keep most recent within budget
        tokens = _estimate_tokens(row.body)
        if used + tokens > token_budget:
            break
        kept.append(row)
        used += tokens
    kept.reverse()  # back to chronological order

    history_lines = [
        f"  [{row.role.upper()}] {row.body}" for row in kept
    ] or ["  (no prior messages)"]

    # User criteria
    criteria_row = db.query(UserCriteria).filter_by(user_id=user.id).first()
    criteria_str = (
        json.dumps(criteria_row.criteria, indent=2)
        if criteria_row
        else "(not set)"
    )

    return (
        f"**User's message:**\n{user_message}\n\n"
        f"**Pending notifications (slots you've recently told this user about):**\n"
        + ("\n".join(pending_lines) if pending_lines else "  (none)")
        + f"\n\n**User's preferences:**\n{criteria_str}"
        + f"\n\n**Conversation history (last 14 days):**\n"
        + "\n".join(history_lines)
    )


# ---------------------------------------------------------------------------
# Agentic loop
# ---------------------------------------------------------------------------

async def handle_reply(user: User, message: str, db: Session) -> str:
    """
    Process an inbound user message and return a reply SMS body.

    Runs the agentic loop: calls Claude with tools, executes any tool calls,
    feeds results back, repeats until the model produces a final text response
    or MAX_ROUNDS is reached.

    DB changes from tool calls are flushed but NOT committed here.
    The caller (webhook) is responsible for commit/rollback.

    Logs the full agent turn (thinking, tools, response) to agent_turns.
    """
    system_prompt = (PROMPTS_DIR / "conversation_system.txt").read_text(encoding="utf-8")
    user_content = _build_user_message(message, user, db)

    messages = [{"role": "user", "content": user_content}]
    all_thinking: list[str] = []
    all_tools: list[dict] = []
    reply = "I'm not sure how to help with that. Reply STOP to unsubscribe."
    t0 = time.perf_counter()

    for round_num in range(MAX_ROUNDS):
        t_round = time.perf_counter()
        response = await _get_client().messages.create(
            model=MODEL,
            max_tokens=4096,
            thinking={"type": "enabled", "budget_tokens": THINKING_BUDGET},
            system=system_prompt,
            tools=TOOLS,
            messages=messages,
        )

        # Capture thinking blocks from this round
        for block in response.content:
            if block.type == "thinking" and block.thinking:
                all_thinking.append(f"[Round {round_num + 1}]\n{block.thinking}")

        if response.stop_reason == "end_turn":
            # Extract the final text response
            for block in response.content:
                if block.type == "text" and block.text.strip():
                    reply = block.text.strip()
                    break
            break

        if response.stop_reason == "tool_use":
            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                result = _execute_tool(block.name, block.input, user, db)
                all_tools.append({
                    "round": round_num + 1,
                    "tool": block.name,
                    "input": block.input,
                    "output": result,
                })
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(result),
                })

            # Append assistant's message and tool results for next round
            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user", "content": tool_results})
        else:
            # Unexpected stop reason — bail out
            logger.warning(
                "Unexpected stop_reason %r for user %d — ending loop",
                response.stop_reason,
                user.id,
            )
            break

    # If the user is paused, re-activate them when they reply
    if user.status == "paused":
        # Check if pause_notifications was NOT called this turn (otherwise leave paused)
        paused_this_turn = any(t["tool"] == "pause_notifications" for t in all_tools)
        if not paused_this_turn:
            user.status = "active"
            db.flush()
            logger.info("Reactivated paused user %d on reply", user.id)

    # Log the agent turn for observability
    _log_agent_turn(
        user_id=user.id,
        message_in=message,
        thinking_text="\n\n---\n\n".join(all_thinking) or None,
        tools_called=all_tools or None,
        response_out=reply,
        db=db,
    )

    logger.info(
        "Conversation agent replied to user %d in %.2fs (%d round(s), %d tool call(s))",
        user.id,
        time.perf_counter() - t0,
        round_num + 1,
        len(all_tools),
    )
    return reply


def _log_agent_turn(
    user_id: int,
    message_in: str,
    thinking_text: str | None,
    tools_called: list | None,
    response_out: str,
    db: Session,
) -> None:
    """Insert an agent_turns row. Failures are logged but never raised."""
    try:
        db.add(
            AgentTurn(
                user_id=user_id,
                message_in=message_in,
                thinking_text=thinking_text,
                tools_called=tools_called,
                response_out=response_out,
                model=MODEL,
            )
        )
        db.flush()
    except Exception as e:
        logger.error("Failed to log agent turn for user %d: %s: %s", user_id, type(e).__name__, e)

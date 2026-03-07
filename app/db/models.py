"""
SQLAlchemy ORM models for Fjord Ranger.

Tables:
  users             — registered users and their notification settings
  user_criteria     — each user's slot preferences (JSONB, one row per user)
  slots             — canonical registry of every Momence session seen
  user_slot_states  — per-user state machine for each slot
  notifications     — outbound SMS log, maps slot_codes to slots for reply disambiguation
  messages          — full conversation thread per user (user + assistant turns)
  agent_turns       — observability log: LLM thinking, tool calls, and responses
"""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

class User(Base):
    """
    A registered Fjord Ranger user.

    status values:
      active     — receiving notifications
      paused     — temporarily opted out (user-requested)
      opted_out  — permanently unsubscribed (STOP or explicit request)
    """

    __tablename__ = "users"

    id                          = Column(Integer, primary_key=True)
    phone_number                = Column(String(20), unique=True, nullable=False)
    status                      = Column(String(20), nullable=False, default="active")
    max_notifications_per_day   = Column(SmallInteger, nullable=False, default=3)
    daily_notification_count    = Column(SmallInteger, nullable=False, default=0)
    daily_count_reset_at        = Column(Date, nullable=False, server_default=func.current_date())
    preferences_nudge_sent      = Column(Boolean, nullable=False, default=False, server_default="false")
    created_at                  = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    # Relationships
    criteria        = relationship("UserCriteria", back_populates="user", uselist=False)
    slot_states     = relationship("UserSlotState", back_populates="user")
    notifications   = relationship("Notification", back_populates="user")
    messages        = relationship("Message", back_populates="user")
    agent_turns     = relationship("AgentTurn", back_populates="user")

    def __repr__(self):
        return f"<User id={self.id} phone={self.phone_number} status={self.status}>"

    @property
    def is_notifiable(self) -> bool:
        """True if we can send this user a notification right now."""
        return (
            self.status == "active"
            and self.daily_notification_count < self.max_notifications_per_day
        )


# ---------------------------------------------------------------------------
# User Criteria
# ---------------------------------------------------------------------------

class UserCriteria(Base):
    """
    A user's slot preferences, stored as JSONB so the schema can evolve
    without migrations every time a new preference type is added.

    One row per user (1:1 with User).

    Example criteria value:
    {
        "preferred_days": ["Saturday", "Sunday"],
        "time_window": {"earliest": "08:00", "latest": "13:00"},
        "session_names": ["Shared Session (90 Min)"],
        "min_spots": 1
    }

    session_names matches the Momence API's sessionName field exactly:
      "Private Session (North)"
      "Private Session (South)"
      "Shared Session (90 Min)"
    """

    __tablename__ = "user_criteria"

    user_id     = Column(Integer, ForeignKey("users.id"), primary_key=True)
    criteria    = Column(JSONB, nullable=False)
    updated_at  = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    user = relationship("User", back_populates="criteria")

    def __repr__(self):
        return f"<UserCriteria user_id={self.user_id}>"


# ---------------------------------------------------------------------------
# Slots
# ---------------------------------------------------------------------------

class Slot(Base):
    """
    Canonical registry of every Momence session the scraper has seen.

    momence_id is Momence's own stable integer ID. It appears directly in
    the booking URL: https://momence.com/s/{momence_id}

    Known session_name values (as of 2026-02-28):
      "Private Session (North)"  — $270, 120 min, Sauna #2 (North), capacity 1
      "Private Session (South)"  — $270, 120 min, Sauna #1 (South), capacity 1
      "Shared Session (90 Min)"  — $45,  90 min,  North & South,    capacity 8
    """

    __tablename__ = "slots"

    momence_id          = Column(BigInteger, primary_key=True)
    session_name        = Column(String(100), nullable=False)
    starts_at           = Column(DateTime(timezone=True), nullable=False)
    ends_at             = Column(DateTime(timezone=True), nullable=False)
    duration_minutes    = Column(SmallInteger, nullable=False)
    location            = Column(String(100), nullable=False)
    location_id         = Column(Integer, nullable=False)
    price_usd           = Column(Numeric(8, 2), nullable=False)
    capacity            = Column(SmallInteger, nullable=False)
    remaining_spots     = Column(SmallInteger, nullable=False, default=0)
    total_spots         = Column(SmallInteger, nullable=False)
    is_cancelled        = Column(Boolean, nullable=False, default=False)
    booking_url         = Column(Text, nullable=False)
    first_seen_at       = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    last_seen_at        = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    # Relationships
    user_states     = relationship("UserSlotState", back_populates="slot")
    notifications   = relationship("Notification", back_populates="slot")

    def __repr__(self):
        return f"<Slot id={self.momence_id} name={self.session_name!r} starts={self.starts_at}>"

    @property
    def is_available(self) -> bool:
        return not self.is_cancelled and self.remaining_spots > 0


# ---------------------------------------------------------------------------
# User Slot States  (the state machine)
# ---------------------------------------------------------------------------

class UserSlotState(Base):
    """
    Per-user state for each slot.

    State machine:
      new        → notified   (analyzer matched slot to criteria, SMS sent)
      notified   → rejected   (user replied NO-<code>)
      notified   → expired    (slot's starts_at passed without user action)
      new        → expired    (slot passed before we had a chance to notify)
      rejected   → new        (user explicitly resets their preferences)

    Transitions owned by:
      new → notified:   scrape_job / analyzer
      notified → rejected: webhook handler (on NO-<code> reply)
      * → expired:      nightly janitor job
    """

    __tablename__ = "user_slot_states"

    user_id         = Column(Integer, ForeignKey("users.id"), primary_key=True)
    momence_id      = Column(BigInteger, ForeignKey("slots.momence_id"), primary_key=True)
    state           = Column(String(20), nullable=False, default="new")
    notified_at     = Column(DateTime(timezone=True), nullable=True)
    feedback_raw    = Column(Text, nullable=True)   # verbatim SMS reply from user
    updated_at      = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    user    = relationship("User", back_populates="slot_states")
    slot    = relationship("Slot", back_populates="user_states")

    def __repr__(self):
        return f"<UserSlotState user={self.user_id} slot={self.momence_id} state={self.state}>"


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------

class Notification(Base):
    """
    Log of every outbound SMS notification sent to a user.

    slot_code is a short alphanumeric code (e.g. "A3F2K1") included in the
    SMS body so the user can reference it in their reply:
      "Reply NO-A3F2K1 to skip this slot."

    The webhook handler looks up slot_code to identify which slot and user
    a reply refers to (reply disambiguation).
    """

    __tablename__ = "notifications"

    id                  = Column(Integer, primary_key=True, autoincrement=True)
    slot_code           = Column(String(6), unique=True, nullable=False)
    user_id             = Column(Integer, ForeignKey("users.id"), nullable=False)
    momence_id          = Column(BigInteger, ForeignKey("slots.momence_id"), nullable=False)
    twilio_message_sid  = Column(String(50), nullable=True)   # null until Twilio confirms send
    sent_at             = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    user    = relationship("User", back_populates="notifications")
    slot    = relationship("Slot", back_populates="notifications")

    def __repr__(self):
        return f"<Notification id={self.id} code={self.slot_code} user={self.user_id} slot={self.momence_id}>"


# ---------------------------------------------------------------------------
# Messages  (conversation thread)
# ---------------------------------------------------------------------------

class Message(Base):
    """
    One turn in the SMS conversation between the user and the agent.

    role values:
      user       — inbound SMS from the user
      assistant  — outbound SMS from Fjord Ranger (notifications and replies)

    Stored for every exchange so the conversation agent has full history
    and so you can replay and study the conversation flow.
    """

    __tablename__ = "messages"

    id          = Column(Integer, primary_key=True, autoincrement=True)
    user_id     = Column(Integer, ForeignKey("users.id"), nullable=False)
    role        = Column(String(10), nullable=False)   # 'user' | 'assistant'
    body        = Column(Text, nullable=False)
    created_at  = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    user = relationship("User", back_populates="messages")

    def __repr__(self):
        return f"<Message id={self.id} user={self.user_id} role={self.role}>"


# ---------------------------------------------------------------------------
# Agent Turns  (observability log)
# ---------------------------------------------------------------------------

class AgentTurn(Base):
    """
    Full observability record for every conversation agent invocation.

    Captures the LLM's extended thinking, every tool call with its inputs
    and outputs, and the final response. Use this to study and tune the
    agent's decision-making.

    thinking_text  — raw extended thinking block(s), concatenated across rounds
    tools_called   — JSONB array: [{tool, input, output}, ...]
    """

    __tablename__ = "agent_turns"

    id              = Column(Integer, primary_key=True, autoincrement=True)
    user_id         = Column(Integer, ForeignKey("users.id"), nullable=False)
    message_in      = Column(Text, nullable=False)
    thinking_text   = Column(Text, nullable=True)
    tools_called    = Column(JSONB, nullable=True)
    response_out    = Column(Text, nullable=False)
    model           = Column(String(50), nullable=False)
    created_at      = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    user = relationship("User", back_populates="agent_turns")

    def __repr__(self):
        return f"<AgentTurn id={self.id} user={self.user_id}>"

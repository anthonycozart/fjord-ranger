"""
Admin CLI for Fjord Ranger.

Usage:
  venv/bin/python scripts/admin.py list-users
  venv/bin/python scripts/admin.py add-user +14155551234
  venv/bin/python scripts/admin.py set-status +14155551234 active
  venv/bin/python scripts/admin.py show-criteria +14155551234
  venv/bin/python scripts/admin.py set-criteria +14155551234 '{"preferred_days":["Saturday","Sunday"],"time_window":{"earliest":"08:00","latest":"13:00"},"session_names":["Shared Session (90 Min)"],"min_spots":1}'
  venv/bin/python scripts/admin.py show-slots --available
  venv/bin/python scripts/admin.py show-notifications +14155551234
  venv/bin/python scripts/admin.py stats
"""

import json
import sys
import os
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv
load_dotenv()

from sqlalchemy import func
from app.db.database import get_session_factory
from app.db.models import AgentTurn, Message, Notification, Slot, User, UserCriteria, UserSlotState

SEP = "-" * 70


def get_db():
    return get_session_factory()()


def _criteria_summary(criteria_row) -> str:
    if criteria_row is None or not criteria_row.criteria:
        return "(none set)"
    c = criteria_row.criteria
    parts = []
    if c.get("session_names"):
        types = set()
        for n in c["session_names"]:
            if "Private" in n:
                types.add("Private")
            elif "Shared" in n:
                types.add("Shared")
        parts.append("/".join(sorted(types)))
    if c.get("preferred_days"):
        parts.append(" ".join(d[:3] for d in c["preferred_days"]))
    if c.get("time_window"):
        tw = c["time_window"]
        parts.append(f"{tw.get('earliest', '?')}–{tw.get('latest', '?')}")
    if c.get("bulk_release_alerts"):
        parts.append("bulk✓")
    return ", ".join(parts) or "(empty)"


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_list_users():
    db = get_db()
    users = db.query(User).order_by(User.created_at).all()
    if not users:
        print("No users.")
        return
    print(f"\n{'ID':<5} {'Phone':<18} {'Status':<12} {'Daily':<8} {'Nudge':<7} Criteria")
    print(SEP)
    for u in users:
        daily = f"{u.daily_notification_count}/{u.max_notifications_per_day}"
        nudge = "sent" if u.preferences_nudge_sent else "no"
        print(
            f"{u.id:<5} {u.phone_number:<18} {u.status:<12} "
            f"{daily:<8} {nudge:<7} {_criteria_summary(u.criteria)}"
        )
    db.close()


def cmd_add_user(phone: str):
    db = get_db()
    existing = db.query(User).filter_by(phone_number=phone).first()
    if existing:
        print(f"User {phone} already exists (status: {existing.status})")
        db.close()
        return
    user = User(phone_number=phone, status="active")
    db.add(user)
    db.commit()
    print(f"Added user {phone} (id={user.id}, status=active)")
    db.close()


def cmd_set_status(phone: str, status: str):
    valid = {"active", "paused", "opted_out"}
    if status not in valid:
        print(f"Invalid status '{status}'. Choose from: {', '.join(sorted(valid))}")
        return
    db = get_db()
    user = db.query(User).filter_by(phone_number=phone).first()
    if not user:
        print(f"No user found with phone {phone}")
        db.close()
        return
    user.status = status
    db.commit()
    print(f"Updated {phone} → status={status}")
    db.close()


def cmd_show_criteria(phone: str):
    db = get_db()
    user = db.query(User).filter_by(phone_number=phone).first()
    if not user:
        print(f"No user found with phone {phone}")
        db.close()
        return
    if not user.criteria:
        print(f"No criteria set for {phone}")
        db.close()
        return
    print(json.dumps(user.criteria.criteria, indent=2))
    db.close()


def cmd_set_criteria(phone: str, criteria_json: str):
    try:
        criteria = json.loads(criteria_json)
    except json.JSONDecodeError as e:
        print(f"Invalid JSON: {e}")
        return
    db = get_db()
    user = db.query(User).filter_by(phone_number=phone).first()
    if not user:
        print(f"No user found with phone {phone}")
        db.close()
        return
    if user.criteria:
        user.criteria.criteria = criteria
    else:
        db.add(UserCriteria(user_id=user.id, criteria=criteria))
    db.commit()
    print(f"Criteria set for {phone}")
    db.close()


def cmd_show_slots(available_only: bool = False):
    db = get_db()
    q = db.query(Slot).order_by(Slot.starts_at)
    if available_only:
        q = q.filter(Slot.remaining_spots > 0, Slot.is_cancelled == False)
    slots = q.limit(20).all()
    if not slots:
        print("No slots found.")
        db.close()
        return
    print(f"\n{'ID':<12} {'Session':<28} {'Starts (PT)':<22} {'Spots':<8} Status")
    print(SEP)
    from zoneinfo import ZoneInfo
    PT = ZoneInfo("America/Los_Angeles")
    for s in slots:
        starts_pt = s.starts_at.astimezone(PT).strftime("%a %b %-d %-I:%M %p")
        spots = f"{s.remaining_spots}/{s.total_spots}"
        status = "Available" if s.is_available else ("Cancelled" if s.is_cancelled else "Full")
        print(f"{s.momence_id:<12} {s.session_name:<28} {starts_pt:<22} {spots:<8} {status}")
    db.close()


def cmd_show_notifications(phone: str):
    db = get_db()
    user = db.query(User).filter_by(phone_number=phone).first()
    if not user:
        print(f"No user found with phone {phone}")
        db.close()
        return
    notifs = (
        db.query(Notification, Slot)
        .join(Slot, Notification.momence_id == Slot.momence_id)
        .filter(Notification.user_id == user.id)
        .order_by(Notification.sent_at.desc())
        .limit(10)
        .all()
    )
    if not notifs:
        print(f"No notifications for {phone}")
        db.close()
        return
    print(f"\nLast {len(notifs)} notifications for {phone}:")
    for notif, slot in notifs:
        state_row = db.query(UserSlotState).filter_by(
            user_id=user.id, momence_id=slot.momence_id
        ).first()
        state = state_row.state if state_row else "?"
        print(
            f"  [{notif.sent_at.strftime('%Y-%m-%d %H:%M')}] "
            f"[{notif.slot_code}] {slot.session_name} · "
            f"{slot.starts_at.strftime('%a %b %-d')} · {state}"
        )
    db.close()


def cmd_stats():
    db = get_db()
    try:
        now = datetime.now(timezone.utc)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        week_start = today_start - timedelta(days=7)

        print(f"\nBACKEND STATS  ({now.strftime('%b %-d %Y  %-I:%M %p UTC')})")
        print(SEP)

        # Users
        users = db.query(User).all()
        status_counts: dict[str, int] = {}
        for u in users:
            status_counts[u.status] = status_counts.get(u.status, 0) + 1
        print("USERS")
        for status in ("active", "paused", "opted_out"):
            print(f"  {status:<18} {status_counts.get(status, 0)}")
        print(f"  {'Total':<18} {len(users)}")

        # Notifications
        total_notifs = db.query(func.count(Notification.id)).scalar() or 0
        today_notifs = db.query(func.count(Notification.id)).filter(
            Notification.sent_at >= today_start
        ).scalar() or 0
        week_notifs = db.query(func.count(Notification.id)).filter(
            Notification.sent_at >= week_start
        ).scalar() or 0
        print("\nNOTIFICATIONS")
        print(f"  {'Total sent':<18} {total_notifs:,}")
        print(f"  {'Today':<18} {today_notifs:,}")
        print(f"  {'Last 7 days':<18} {week_notifs:,}")

        # Agent turns & tokens
        turns = db.query(AgentTurn).all()
        pref_updates = sum(
            1 for t in turns
            if any(tc.get("tool") == "update_user_criteria" for tc in (t.tools_called or []))
        )
        total_input = sum(t.input_tokens or 0 for t in turns)
        total_output = sum(t.output_tokens or 0 for t in turns)
        print("\nCONVERSATION AGENT")
        print(f"  {'Total turns':<18} {len(turns):,}")
        print(f"  {'Pref updates':<18} {pref_updates:,}")
        print(f"  {'Input tokens':<18} {total_input:,}")
        print(f"  {'Output tokens':<18} {total_output:,}")

        # Messages
        msg_rows = db.query(Message).all()
        total_msgs = len(msg_rows)
        avg_len = int(sum(len(m.body) for m in msg_rows) / total_msgs) if total_msgs else 0
        print("\nMESSAGES")
        print(f"  {'Total':<18} {total_msgs:,}")
        print(f"  {'Avg length (chars)':<18} {avg_len:,}")

        # Slots
        total_slots = db.query(func.count(Slot.momence_id)).scalar() or 0
        available_slots = db.query(func.count(Slot.momence_id)).filter(
            Slot.remaining_spots > 0,
            Slot.is_cancelled == False,
            Slot.starts_at > now,
        ).scalar() or 0
        print("\nSLOTS")
        print(f"  {'Total seen':<18} {total_slots:,}")
        print(f"  {'Available now':<18} {available_slots:,}")

        print()
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

COMMANDS = {
    "list-users":         (cmd_list_users,        []),
    "add-user":           (cmd_add_user,           ["phone"]),
    "set-status":         (cmd_set_status,         ["phone", "status"]),
    "show-criteria":      (cmd_show_criteria,      ["phone"]),
    "set-criteria":       (cmd_set_criteria,       ["phone", "criteria_json"]),
    "show-slots":         (cmd_show_slots,         []),
    "show-notifications": (cmd_show_notifications, ["phone"]),
    "stats":              (cmd_stats,              []),
}

if __name__ == "__main__":
    args = sys.argv[1:]
    if not args or args[0] not in COMMANDS:
        print("Usage: venv/bin/python scripts/admin.py <command> [args]")
        print("Commands:")
        for name in COMMANDS:
            print(f"  {name}")
        sys.exit(1)

    cmd_name = args[0]
    fn, params = COMMANDS[cmd_name]
    cmd_args = args[1:]

    if cmd_name == "show-slots":
        fn(available_only="--available" in cmd_args)
    elif cmd_name in ("list-users", "stats"):
        fn()
    elif len(cmd_args) != len(params):
        print(f"Usage: venv/bin/python scripts/admin.py {cmd_name} {' '.join(params)}")
        sys.exit(1)
    else:
        fn(*cmd_args)

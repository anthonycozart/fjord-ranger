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
"""

import json
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv
load_dotenv()

from app.db.database import get_session_factory
from app.db.models import User, UserCriteria, Slot, UserSlotState, Notification


def get_db():
    return get_session_factory()()


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_list_users():
    db = get_db()
    users = db.query(User).order_by(User.created_at).all()
    if not users:
        print("No users.")
        return
    print(f"{'ID':<4} {'Phone':<16} {'Status':<10} {'Notifs today':<14} {'Created'}")
    print("-" * 65)
    for u in users:
        print(f"{u.id:<4} {u.phone_number:<16} {u.status:<10} "
              f"{u.daily_notification_count}/{u.max_notifications_per_day:<12} "
              f"{u.created_at.strftime('%Y-%m-%d')}")
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
    valid = {"pending", "active", "paused", "opted_out"}
    if status not in valid:
        print(f"Invalid status '{status}'. Choose from: {', '.join(valid)}")
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
    print(f"{'ID':<12} {'Session':<28} {'Starts (PT)':<22} {'Spots':<8} {'Status'}")
    print("-" * 85)
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
    notifs = (db.query(Notification)
              .filter_by(user_id=user.id)
              .order_by(Notification.sent_at.desc())
              .limit(10).all())
    if not notifs:
        print(f"No notifications for {phone}")
        db.close()
        return
    print(f"Last {len(notifs)} notifications for {phone}:")
    for n in notifs:
        print(f"  [{n.sent_at.strftime('%Y-%m-%d %H:%M')}] code={n.slot_code} slot={n.momence_id}")
    db.close()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

COMMANDS = {
    "list-users":         (cmd_list_users,       []),
    "add-user":           (cmd_add_user,          ["phone"]),
    "set-status":         (cmd_set_status,        ["phone", "status"]),
    "show-criteria":      (cmd_show_criteria,     ["phone"]),
    "set-criteria":       (cmd_set_criteria,      ["phone", "criteria_json"]),
    "show-slots":         (cmd_show_slots,        []),
    "show-notifications": (cmd_show_notifications,["phone"]),
}

if __name__ == "__main__":
    args = sys.argv[1:]
    if not args or args[0] not in COMMANDS:
        print("Usage: python scripts/admin.py <command> [args]")
        print("Commands:")
        for name in COMMANDS:
            print(f"  {name}")
        sys.exit(1)

    cmd_name = args[0]
    fn, params = COMMANDS[cmd_name]
    cmd_args = args[1:]

    # Special flag handling
    if cmd_name == "show-slots":
        fn(available_only="--available" in cmd_args)
    elif len(cmd_args) != len(params):
        print(f"Usage: python scripts/admin.py {cmd_name} {' '.join(params)}")
        sys.exit(1)
    else:
        fn(*cmd_args)

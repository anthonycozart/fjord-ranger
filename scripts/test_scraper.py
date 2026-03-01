"""
Quick smoke test for the Momence scraper.
Run with: venv/bin/python scripts/test_scraper.py
"""

import asyncio
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import logging
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

from app.scraper.momence import fetch_all_sessions, fetch_available_sessions


async def main():
    print("--- Fetching ALL upcoming sessions ---")
    all_sessions = await fetch_all_sessions()
    print(f"Total sessions: {len(all_sessions)}\n")

    # Show session type breakdown
    from collections import Counter
    types = Counter(s.session_name for s in all_sessions)
    print("Session types:")
    for name, count in types.most_common():
        print(f"  {count:3d}x  {name}")

    print("\n--- Available sessions (spots > 0) ---")
    available = [s for s in all_sessions if s.is_available]
    print(f"Available: {len(available)}/{len(all_sessions)}\n")

    if available:
        print("First 10 available:")
        for s in available[:10]:
            print(f"  {s.describe()}")
            print(f"    → {s.booking_url}")
    else:
        print("(No slots available right now — all full)")
        print("\nNext 5 upcoming slots regardless:")
        for s in all_sessions[:5]:
            print(f"  {s.describe()}")
            print(f"    → {s.booking_url}")

    # Show date range covered
    if all_sessions:
        first = all_sessions[0].starts_at_pt.strftime("%Y-%m-%d")
        last = all_sessions[-1].starts_at_pt.strftime("%Y-%m-%d")
        print(f"\nDate range: {first} → {last}")


if __name__ == "__main__":
    asyncio.run(main())

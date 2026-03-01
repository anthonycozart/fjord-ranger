"""
Live test for the analyzer: fetches real slots from Momence and runs
a handful of criteria checks against Claude to confirm prompt and
parsing work correctly.

Run with: venv/bin/python scripts/test_analyzer.py
"""

import asyncio
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv
load_dotenv()

from app.scraper.momence import fetch_all_sessions
from app.agent.analyzer import matches_criteria

# Test criteria scenarios
CRITERIA_SCENARIOS = [
    {
        "label": "Weekend mornings, shared only",
        "criteria": {
            "preferred_days": ["Saturday", "Sunday"],
            "time_window": {"earliest": "08:00", "latest": "13:00"},
            "session_names": ["Shared Session (90 Min)"],
            "min_spots": 1,
        },
    },
    {
        "label": "Any day, private north only",
        "criteria": {
            "session_names": ["Private Session (North)"],
        },
    },
    {
        "label": "Weekday evenings",
        "criteria": {
            "preferred_days": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"],
            "time_window": {"earliest": "17:00", "latest": "22:00"},
        },
    },
]


async def main():
    print("Fetching sessions from Momence...")
    sessions = await fetch_all_sessions()
    available = [s for s in sessions if s.is_available]
    all_sessions = sessions[:5]  # test against first 5 regardless of availability

    print(f"Total: {len(sessions)} sessions, {len(available)} available")
    print(f"Testing analyzer against first {len(all_sessions)} sessions\n")

    for scenario in CRITERIA_SCENARIOS:
        print(f"=== Scenario: {scenario['label']} ===")
        print(f"Criteria: {scenario['criteria']}\n")

        for session in all_sessions:
            result = await matches_criteria(session, scenario["criteria"])
            status = "MATCH ✓" if result.matches else "no match"
            print(f"  [{status}] {session.describe()}")
            print(f"           → {result.reasoning}")
        print()


if __name__ == "__main__":
    asyncio.run(main())

"""
Fjord Ranger — main entry point.

Starts two APScheduler jobs:
  - run_scrape_cycle: every 5 minutes, 6am–10pm PT
  - check_dead_mans_switch: every 2 hours

Also mounts the FastAPI app for the Twilio SMS webhook.
"""

import logging
import os

from dotenv import load_dotenv
load_dotenv()  # loads .env in local dev; no-op in Railway (env vars already set)

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import FastAPI
from zoneinfo import ZoneInfo

from app.jobs.scrape_job import check_dead_mans_switch, run_scrape_cycle
from app.notifications.webhook import router as sms_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

PT = ZoneInfo("America/Los_Angeles")

app = FastAPI(title="Fjord Ranger")
scheduler = AsyncIOScheduler(timezone=PT)


@app.on_event("startup")
async def startup():
    # Scrape every 5 minutes, 6am–10pm PT only
    scheduler.add_job(
        run_scrape_cycle,
        trigger=CronTrigger(
            hour="6-21",        # 6:00am to 9:55pm (last fire at 9:55, ends before 10pm)
            minute="*/5",
            timezone=PT,
        ),
        id="scrape_cycle",
        name="Momence scrape",
        max_instances=1,        # never run two scrapes simultaneously
        misfire_grace_time=60,  # skip if delayed >60s rather than catching up
    )

    # Dead-man's switch: check every 2 hours, all day
    scheduler.add_job(
        check_dead_mans_switch,
        trigger=IntervalTrigger(hours=2),
        id="dead_mans_switch",
        name="Dead-man's switch",
    )

    scheduler.start()
    logger.info("Scheduler started. Scrape job: every 5 min, 6am–10pm PT.")


@app.on_event("shutdown")
async def shutdown():
    scheduler.shutdown(wait=False)
    logger.info("Scheduler stopped.")


@app.get("/health")
async def health():
    """Health check endpoint for Railway."""
    from app.jobs.scrape_job import _last_success_at, _consecutive_failures
    return {
        "status": "ok",
        "last_scrape_utc": _last_success_at.isoformat() if _last_success_at else None,
        "consecutive_failures": _consecutive_failures,
        "scheduler_running": scheduler.running,
    }


app.include_router(sms_router)

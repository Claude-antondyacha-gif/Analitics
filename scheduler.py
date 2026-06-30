"""
Daily scheduler — runs data collection + AI analysis every day at 07:00.
Run: python scheduler.py
"""
import os
import sys
import logging
from pathlib import Path

from dotenv import load_dotenv
from apscheduler.schedulers.blocking import BlockingScheduler

load_dotenv()

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("data/scheduler.log"),
    ],
)
logger = logging.getLogger(__name__)


def daily_job():
    logger.info("=== DAILY JOB STARTED ===")

    # 1. Collect data from Meta
    try:
        from collector.meta_collector import run_daily_collection
        count = run_daily_collection()
        logger.info(f"Collection done: {count} rows")
    except Exception as e:
        logger.error(f"Collection failed: {e}", exc_info=True)

    # 2. AI Analysis for multiple periods
    try:
        from agent.ai_agent import analyze, execute_suggested_actions
        for days, label in [(1, "1d"), (7, "7d"), (30, "30d")]:
            result = analyze(period_label=label, days=days)
            logger.info(f"AI analysis {label}: {len(result.get('recommendations', []))} recs, "
                        f"{len(result.get('critical_alerts', []))} alerts")

            # Auto-execute only critical pauses (safety: disabled by default)
            execute_suggested_actions(result, auto_execute=False)

    except Exception as e:
        logger.error(f"AI analysis failed: {e}", exc_info=True)

    # 3. Sync to Google Sheets
    try:
        from reports.sheets_sync import sync_to_sheets
        sync_to_sheets(days=30)
        logger.info("Google Sheets synced")
    except Exception as e:
        logger.warning(f"Sheets sync failed (non-critical): {e}")

    # 4. Send Telegram report
    try:
        from notifier.telegram_bot import send_daily_report
        ok = send_daily_report()
        logger.info(f"Telegram report sent: {ok}")
    except Exception as e:
        logger.warning(f"Telegram send failed (non-critical): {e}")

    logger.info("=== DAILY JOB DONE ===")


def run_now():
    """Run the full pipeline immediately (for testing)."""
    from storage.database import init_db
    init_db()
    daily_job()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--now", action="store_true", help="Run job immediately")
    parser.add_argument("--backfill", type=int, metavar="DAYS", help="Backfill last N days")
    args = parser.parse_args()

    os.makedirs("data", exist_ok=True)

    if args.backfill:
        from dotenv import load_dotenv
        load_dotenv()
        from storage.database import init_db
        from collector.meta_collector import run_historical_backfill
        init_db()
        run_historical_backfill(days=args.backfill)
        sys.exit(0)

    if args.now:
        run_now()
        sys.exit(0)

    # Start scheduler
    from storage.database import init_db
    init_db()

    scheduler = BlockingScheduler(timezone="Europe/Kyiv")
    scheduler.add_job(daily_job, "cron", hour=7, minute=0, id="daily_collection")
    logger.info("Scheduler started. Daily job runs at 07:00 Kyiv time.")
    logger.info("Press Ctrl+C to stop.")

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Scheduler stopped.")

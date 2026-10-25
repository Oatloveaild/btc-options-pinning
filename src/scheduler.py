import logging
from datetime import datetime
from apscheduler.schedulers.background import BackgroundScheduler
from src.collector import run_snapshot
from src.config import BKK_TZ
from src.db import log_execution
from src.settle import run_settlement

logger = logging.getLogger("scheduler")

scheduler = BackgroundScheduler(timezone=BKK_TZ)


def job_0600():
    logger.info("Executing 06:00 scheduled snapshot (today's expiry)...")
    try:
        run_snapshot(label="0600")
        from src.gdrive_sync import upload_db_to_gdrive
        upload_db_to_gdrive()
    except Exception as e:
        logger.error(f"Error in 06:00 job: {e}")


def job_1530():
    logger.info("Executing 15:30 scheduled settlement & snapshot (tomorrow's expiry)...")
    ref_time_str = datetime.now(BKK_TZ).strftime("%Y-%m-%d %H:%M:%S")
    # 1. Settle today's expiry
    try:
        run_settlement()
    except Exception as e:
        logger.error(f"Error in 15:30 settlement: {e}")
        log_execution(ref_time_str, "1530", "settle", "ERROR", str(e))

    # 2. Snapshot tomorrow's expiry
    try:
        run_snapshot(label="1530")
        from src.gdrive_sync import upload_db_to_gdrive
        upload_db_to_gdrive()
    except Exception as e:
        logger.error(f"Error in 15:30 snapshot: {e}")
        log_execution(ref_time_str, "1530", "snapshot", "ERROR", str(e))


def job_2100():
    logger.info("Executing 21:00 scheduled snapshot (tomorrow's expiry)...")
    try:
        run_snapshot(label="2100")
        from src.gdrive_sync import upload_db_to_gdrive
        upload_db_to_gdrive()
    except Exception as e:
        logger.error(f"Error in 21:00 job: {e}")


def job_hourly():
    now_bkk = datetime.now(BKK_TZ)
    hour = now_bkk.hour
    minute = now_bkk.minute

    # If it's close to 06:00, 15:30, or 21:00, the dedicated jobs will handle it
    if hour == 6 and minute < 10:
        return
    if hour == 21 and minute < 10:
        return

    label = f"hourly_{hour:02d}00"
    logger.info(f"Executing hourly scheduled snapshot ({label})...")
    try:
        run_snapshot(label=label)
        from src.gdrive_sync import upload_db_to_gdrive
        upload_db_to_gdrive()
    except Exception as e:
        logger.error(f"Error in hourly snapshot ({label}): {e}")


def init_scheduler():
    if not scheduler.running:
        # Milestone jobs
        scheduler.add_job(job_0600, "cron", hour=6, minute=0, id="job_0600", replace_existing=True)
        scheduler.add_job(job_1530, "cron", hour=15, minute=30, id="job_1530", replace_existing=True)
        scheduler.add_job(job_2100, "cron", hour=21, minute=0, id="job_2100", replace_existing=True)
        # Hourly job at minute 0 of every hour
        scheduler.add_job(job_hourly, "cron", minute=0, id="job_hourly", replace_existing=True)
        scheduler.start()
        logger.info("APScheduler started with hourly snapshots + milestone jobs (06:00, 15:30, 21:00 Asia/Bangkok).")


def trigger_manual(action: str = "snapshot") -> dict:
    """Manually trigger either a snapshot or settlement right now."""
    if action == "settle":
        res = run_settlement()
        try:
            from src.gdrive_sync import upload_db_to_gdrive
            upload_db_to_gdrive()
        except Exception as e:
            logger.warning(f"Google Drive sync warning: {e}")
        return res
    else:
        now_bkk = datetime.now(BKK_TZ)
        label = f"manual_{now_bkk.strftime('%H%M')}"
        res = run_snapshot(label=label)
        try:
            from src.gdrive_sync import upload_db_to_gdrive
            upload_db_to_gdrive()
        except Exception as e:
            logger.warning(f"Google Drive sync warning: {e}")
        return res

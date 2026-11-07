import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

import backup
import settings_store
from database import SessionLocal

logger = logging.getLogger("scheduler")


def run_backup_job():
    db = SessionLocal()
    try:
        retention = int(settings_store.get(db, "backup_retention"))
        backup.run_backup(retention=retention)
    except Exception:
        logger.exception("Automatic backup failed")
    finally:
        db.close()


def reschedule(scheduler: BackgroundScheduler):
    db = SessionLocal()
    try:
        value = settings_store.get(db, "backup_time")
    finally:
        db.close()
    try:
        h, m = (int(x) for x in value.split(":"))
    except ValueError:
        h, m = 3, 0
    scheduler.add_job(run_backup_job, CronTrigger(hour=h, minute=m), id="backup_job", replace_existing=True)
    logger.info("Daily backup scheduled at %02d:%02d", h, m)


def start_scheduler() -> BackgroundScheduler:
    scheduler = BackgroundScheduler()
    scheduler.start()
    reschedule(scheduler)
    return scheduler

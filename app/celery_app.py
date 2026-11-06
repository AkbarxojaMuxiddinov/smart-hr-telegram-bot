from celery import Celery
from celery.schedules import crontab

from app.config import settings

celery_app = Celery(
    "smart_hr",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=["app.periodic"],
)
celery_app.conf.update(
    timezone=settings.timezone,
    enable_utc=True,
    beat_schedule={
        "checkin-reminder-weekdays": {
            "task": "app.periodic.send_checkin_reminders",
            "schedule": crontab(hour=8, minute=45),
        },
        "daily-hr-report-weekdays": {
            "task": "app.periodic.send_daily_report",
            "schedule": crontab(hour=18, minute=30),
        },
    },
)

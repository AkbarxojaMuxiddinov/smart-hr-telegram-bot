import logging
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import func, select

from app.celery_app import celery_app
from app.config import settings
from app.db import SessionLocal
from app.models import Attendance, Task, User

logger = logging.getLogger(__name__)


def _send_message(chat_id: int, text: str) -> None:
    if not settings.telegram_bot_token:
        logger.warning("Telegram token is not configured; notification skipped")
        return
    response = httpx.post(
        f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage",
        json={"chat_id": chat_id, "text": text},
        timeout=10,
    )
    response.raise_for_status()


@celery_app.task(
    name="app.periodic.send_task_notification",
    autoretry_for=(httpx.HTTPError,),
    retry_backoff=True,
    max_retries=3,
)
def send_task_notification(chat_id: int, title: str) -> None:
    _send_message(chat_id, f"Sizga yangi vazifa biriktirildi: {title}")


@celery_app.task(name="app.periodic.send_checkin_reminders")
def send_checkin_reminders() -> int:
    today = datetime.now(ZoneInfo(settings.timezone)).date()
    with SessionLocal() as db:
        checked_in = select(Attendance.user_id).where(Attendance.work_date == today)
        employees = list(db.scalars(
            select(User).where(
                User.role == "employee",
                User.is_active.is_(True),
                User.telegram_id.is_not(None),
                User.id.not_in(checked_in),
            )
        ))
    sent = 0
    for employee in employees:
        try:
            _send_message(employee.telegram_id, "Ish kunini boshlashni unutmang!")
            sent += 1
        except httpx.HTTPError:
            logger.exception("Reminder send failed for user_id=%s", employee.id)
    return sent


@celery_app.task(name="app.periodic.send_daily_report")
def send_daily_report() -> int:
    today = datetime.now(ZoneInfo(settings.timezone)).date()
    start = datetime.combine(today, time.min, tzinfo=ZoneInfo(settings.timezone))
    end = start + timedelta(days=1)
    with SessionLocal() as db:
        checked_in = db.scalar(select(func.count(Attendance.id)).where(Attendance.work_date == today)) or 0
        late = db.scalar(select(func.count(Attendance.id)).where(
            Attendance.work_date == today, Attendance.status == "late"
        )) or 0
        assigned = db.scalar(select(func.count(Task.id)).where(Task.created_at >= start, Task.created_at < end)) or 0
        completed = db.scalar(select(func.count(Task.id)).where(
            Task.completed_at >= start, Task.completed_at < end
        )) or 0
        total_employees = db.scalar(select(func.count(User.id)).where(
            User.role == "employee", User.is_active.is_(True)
        )) or 0
        recipients = [settings.admin_report_chat_id] if settings.admin_report_chat_id else list(
            db.scalars(select(User.telegram_id).where(
                User.role == "admin", User.is_active.is_(True), User.telegram_id.is_not(None)
            ))
        )
    absent = max(total_employees - checked_in, 0)
    report = (
        f"Kunlik hisobot — {today:%Y-%m-%d}\n"
        f"Xodimlar: {total_employees}\nKelganlar: {checked_in}\nKelmaganlar: {absent}\n"
        f"Kechikkanlar: {late}\nYangi vazifalar: {assigned}\nBajarilgan vazifalar: {completed}"
    )
    sent = 0
    for chat_id in recipients:
        try:
            _send_message(chat_id, report)
            sent += 1
        except httpx.HTTPError:
            logger.exception("Daily report send failed")
    return sent

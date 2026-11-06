import hashlib
import math
import secrets
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Attendance, Office, Task, User


def local_now() -> datetime:
    return datetime.now(ZoneInfo(settings.timezone))


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_invitation() -> tuple[str, str, datetime]:
    token = secrets.token_urlsafe(32)
    return token, token_hash(token), local_now() + timedelta(days=7)


def distance_meters(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    earth_radius = 6_371_000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    value = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * earth_radius * math.atan2(math.sqrt(value), math.sqrt(1 - value))


def check_in(db: Session, user: User, latitude: float, longitude: float) -> Attendance:
    if user.role != "employee":
        raise HTTPException(status_code=400, detail="Davomat faqat xodimlar uchun")
    now = local_now()
    today = now.date()
    existing = db.scalar(select(Attendance).where(Attendance.user_id == user.id, Attendance.work_date == today))
    if existing:
        raise HTTPException(status_code=409, detail="Bugun ish kuni boshlangan")

    offices = list(db.scalars(select(Office).where(Office.is_active.is_(True))))
    if not offices:
        raise HTTPException(status_code=503, detail="Faol ofis manzili sozlanmagan")
    distances = [
        (distance_meters(latitude, longitude, office.latitude, office.longitude), office)
        for office in offices
    ]
    inside_geofences = [(distance, office) for distance, office in distances if distance <= office.radius_meters]
    if not inside_geofences:
        nearest_distance = min(distance for distance, _ in distances)
        raise HTTPException(status_code=403, detail=f"Ofis hududidan tashqaridasiz ({int(nearest_distance)} m)")
    _, nearest = min(inside_geofences, key=lambda item: item[0])

    scheduled = datetime.combine(today, nearest.work_start_time, tzinfo=now.tzinfo)
    late_minutes = max(0, int((now - scheduled).total_seconds() // 60))
    record = Attendance(
        user_id=user.id,
        office_id=nearest.id,
        work_date=today,
        check_in_time=now,
        location_lat=latitude,
        location_lon=longitude,
        status="late" if late_minutes else "on_time",
        late_minutes=late_minutes,
    )
    db.add(record)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Bugun ish kuni boshlangan")
    db.refresh(record)
    return record


def check_out(db: Session, user: User, latitude: float, longitude: float) -> Attendance:
    if user.role != "employee":
        raise HTTPException(status_code=400, detail="Davomat faqat xodimlar uchun")
    record = db.scalar(
        select(Attendance).where(Attendance.user_id == user.id, Attendance.work_date == local_now().date())
    )
    if record is None:
        raise HTTPException(status_code=404, detail="Bugungi check-in topilmadi")
    if record.check_out_time:
        raise HTTPException(status_code=409, detail="Bugun ish kuni tugatilgan")
    record.check_out_time = local_now()
    record.checkout_lat = latitude
    record.checkout_lon = longitude
    db.commit()
    db.refresh(record)
    return record


def submit_task(
    db: Session, task: Task, user: User, proof_file_id: str | None, proof_comment: str | None
) -> Task:
    if task.assigned_to != user.id:
        raise HTTPException(status_code=404, detail="Vazifa topilmadi")
    if not proof_file_id and not (proof_comment and proof_comment.strip()):
        raise HTTPException(status_code=422, detail="Rasm, fayl yoki izoh yuboring")
    if task.status == "completed":
        detail = "Vazifa tekshiruvda" if task.review_status == "pending" else "Vazifa tasdiqlangan"
        raise HTTPException(status_code=409, detail=detail)
    task.proof_file_id = proof_file_id
    task.proof_comment = proof_comment
    task.status = "completed"
    task.review_status = "pending"
    task.review_comment = None
    task.completed_at = local_now()
    db.commit()
    db.refresh(task)
    return task


def start_task(db: Session, task: Task, user: User) -> Task:
    if task.assigned_to != user.id:
        raise HTTPException(status_code=404, detail="Vazifa topilmadi")
    if task.status == "completed":
        raise HTTPException(status_code=409, detail="Vazifa allaqachon topshirilgan")
    if task.status == "pending":
        task.status = "in_progress"
        db.commit()
        db.refresh(task)
    return task


def review_task(db: Session, task: Task, approved: bool, comment: str | None) -> Task:
    if task.review_status != "pending":
        raise HTTPException(status_code=409, detail="Vazifa tasdiqlashni kutmayapti")
    task.review_status = "approved" if approved else "rejected"
    task.status = "completed" if approved else "in_progress"
    task.review_comment = comment
    db.commit()
    db.refresh(task)
    return task


def get_date_range(start: date | None, end: date | None) -> tuple[date, date]:
    today = local_now().date()
    end = end or today
    start = start or end
    if start > end:
        raise HTTPException(status_code=422, detail="Boshlanish sanasi tugash sanasidan keyin")
    return start, end

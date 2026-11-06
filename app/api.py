from datetime import date, datetime, time
from io import BytesIO
import logging
from pathlib import Path
from zoneinfo import ZoneInfo
from xml.sax.saxutils import escape

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.models import Attendance, Office, Task, User
from app.periodic import send_task_notification
from app.schemas import (
    AttendanceOut,
    LoginInput,
    LocationInput,
    MonthlyAttendance,
    OfficeCreate,
    OfficeOut,
    OfficeUpdate,
    TaskCreate,
    TaskOut,
    TaskReview,
    TaskSubmit,
    TokenOutput,
    UserCreate,
    UserCreated,
    UserOut,
    UserUpdate,
)
from app.security import create_access_token, get_current_user, hash_password, require_admin, verify_password
from app.services import (
    check_in,
    check_out,
    get_date_range,
    local_now,
    new_invitation,
    review_task,
    start_task,
    submit_task,
)

router = APIRouter(prefix="/api")
logger = logging.getLogger(__name__)


@router.post("/auth/login", response_model=TokenOutput)
def login(data: LoginInput, db: Session = Depends(get_db)) -> TokenOutput:
    user = db.scalar(select(User).where(User.username == data.username))
    if user is None or not user.is_active or not verify_password(data.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Login yoki parol noto‘g‘ri")
    return TokenOutput(access_token=create_access_token(user))


@router.get("/users/me", response_model=UserOut)
def current_user(user: User = Depends(get_current_user)) -> User:
    return user


@router.get("/users", response_model=list[UserOut])
def list_users(
    role: str | None = Query(default=None, pattern="^(admin|employee)$"),
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
) -> list[User]:
    query = select(User).order_by(User.full_name)
    if role:
        query = query.where(User.role == role)
    return list(db.scalars(query))


@router.post("/users", response_model=UserCreated, status_code=status.HTTP_201_CREATED)
def create_user(
    data: UserCreate,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
) -> UserCreated:
    if data.role == "admin" and (not data.username or not data.password):
        raise HTTPException(status_code=422, detail="Admin uchun username va parol majburiy")
    if data.role == "employee" and data.password:
        raise HTTPException(status_code=422, detail="Xodim botga taklif havolasi orqali ulanadi")
    if data.telegram_id is not None and db.scalar(select(User).where(User.telegram_id == data.telegram_id)):
        raise HTTPException(status_code=409, detail="Telegram ID allaqachon ro‘yxatdan o‘tgan")
    if data.username and db.scalar(select(User).where(User.username == data.username)):
        raise HTTPException(status_code=409, detail="Username band")

    raw_token = None
    link = None
    invite_hash = None
    invite_expires = None
    if data.role == "employee" and data.telegram_id is None:
        raw_token, invite_hash, invite_expires = new_invitation()
        if settings.telegram_bot_username:
            link = f"https://t.me/{settings.telegram_bot_username}?start={raw_token}"
    user = User(
        full_name=data.full_name,
        phone=data.phone,
        role=data.role,
        telegram_id=data.telegram_id,
        username=data.username,
        password_hash=hash_password(data.password) if data.password else None,
        invite_token_hash=invite_hash,
        invite_expires_at=invite_expires,
    )
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Telegram ID yoki username band")
    db.refresh(user)
    return UserCreated(user=user, invite_token=raw_token, telegram_deep_link=link)


@router.patch("/users/{user_id}", response_model=UserOut)
def update_user(
    user_id: int,
    data: UserUpdate,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
) -> User:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="Xodim topilmadi")
    for field, value in data.model_dump(exclude_unset=True, exclude_none=True).items():
        setattr(user, field, value)
    db.commit()
    db.refresh(user)
    return user


@router.post("/users/{user_id}/invite", response_model=UserCreated)
def renew_invitation(
    user_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
) -> UserCreated:
    user = db.get(User, user_id)
    if user is None or user.role != "employee" or user.telegram_id is not None:
        raise HTTPException(status_code=404, detail="Taklif yuborish mumkin bo‘lgan xodim topilmadi")
    if not user.is_active:
        raise HTTPException(status_code=409, detail="Faol bo‘lmagan xodimga taklif yuborib bo‘lmaydi")
    raw_token, user.invite_token_hash, user.invite_expires_at = new_invitation()
    db.commit()
    link = (
        f"https://t.me/{settings.telegram_bot_username}?start={raw_token}"
        if settings.telegram_bot_username
        else None
    )
    return UserCreated(user=user, invite_token=raw_token, telegram_deep_link=link)


@router.get("/offices", response_model=list[OfficeOut])
def list_offices(db: Session = Depends(get_db), _: User = Depends(require_admin)) -> list[Office]:
    return list(db.scalars(select(Office).order_by(Office.name)))


@router.post("/offices", response_model=OfficeOut, status_code=status.HTTP_201_CREATED)
def create_office(
    data: OfficeCreate,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
) -> Office:
    office = Office(**data.model_dump())
    db.add(office)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Bu nomdagi ofis mavjud")
    db.refresh(office)
    return office


@router.patch("/offices/{office_id}", response_model=OfficeOut)
def update_office(
    office_id: int,
    data: OfficeUpdate,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
) -> Office:
    office = db.get(Office, office_id)
    if office is None:
        raise HTTPException(status_code=404, detail="Ofis topilmadi")
    for field, value in data.model_dump(exclude_unset=True, exclude_none=True).items():
        setattr(office, field, value)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Ofis ma’lumotlari takrorlanmoqda")
    db.refresh(office)
    return office


@router.post("/attendance/check-in", response_model=AttendanceOut, status_code=status.HTTP_201_CREATED)
def api_check_in(
    data: LocationInput,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Attendance:
    return check_in(db, user, data.latitude, data.longitude)


@router.post("/attendance/check-out", response_model=AttendanceOut)
def api_check_out(
    data: LocationInput,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Attendance:
    return check_out(db, user, data.latitude, data.longitude)


@router.get("/attendance", response_model=list[AttendanceOut])
def list_attendance(
    date_from: date | None = None,
    date_to: date | None = None,
    user_id: int | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[Attendance]:
    start, end = get_date_range(date_from, date_to)
    query = select(Attendance).where(Attendance.work_date.between(start, end))
    if user.role == "admin":
        if user_id is not None:
            query = query.where(Attendance.user_id == user_id)
    else:
        if user_id is not None and user_id != user.id:
            raise HTTPException(status_code=403, detail="Boshqa xodim ma’lumotiga ruxsat yo‘q")
        query = query.where(Attendance.user_id == user.id)
    return list(db.scalars(query.order_by(Attendance.work_date.desc(), Attendance.check_in_time.desc())))


@router.get("/attendance/monthly", response_model=MonthlyAttendance)
def monthly_attendance(
    year: int | None = Query(default=None, ge=2000, le=2100),
    month: int | None = Query(default=None, ge=1, le=12),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> MonthlyAttendance:
    current = local_now()
    year, month = year or current.year, month or current.month
    first = date(year, month, 1)
    last = date(year + (month == 12), 1 if month == 12 else month + 1, 1)
    rows = list(
        db.scalars(select(Attendance).where(
            Attendance.user_id == user.id,
            Attendance.work_date >= first,
            Attendance.work_date < last,
        ))
    )
    return MonthlyAttendance(
        year=year,
        month=month,
        days_present=len(rows),
        late_days=sum(row.status == "late" for row in rows),
        late_minutes=sum(row.late_minutes for row in rows),
    )


@router.get("/tasks", response_model=list[TaskOut])
def list_tasks(
    status_filter: str | None = Query(default=None, alias="status", pattern="^(pending|in_progress|completed)$"),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[Task]:
    query = select(Task)
    if user.role != "admin":
        query = query.where(Task.assigned_to == user.id)
    if status_filter:
        query = query.where(Task.status == status_filter)
    return list(db.scalars(query.order_by(Task.created_at.desc())))


@router.post("/tasks", response_model=TaskOut, status_code=status.HTTP_201_CREATED)
def create_task(
    data: TaskCreate,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> Task:
    assignee = db.get(User, data.assigned_to)
    if assignee is None or assignee.role != "employee" or not assignee.is_active:
        raise HTTPException(status_code=404, detail="Faol xodim topilmadi")
    task = Task(**data.model_dump(), created_by=admin.id)
    db.add(task)
    db.commit()
    db.refresh(task)
    if assignee.telegram_id and settings.telegram_bot_token:
        try:
            send_task_notification.delay(assignee.telegram_id, task.title)
        except Exception:
            logger.exception("Task notification could not be queued for user_id=%s", assignee.id)
    return task


@router.post("/tasks/{task_id}/submit", response_model=TaskOut)
def submit_task_endpoint(
    task_id: int,
    data: TaskSubmit,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Task:
    task = db.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Vazifa topilmadi")
    return submit_task(db, task, user, data.proof_file_id, data.proof_comment)


@router.post("/tasks/{task_id}/start", response_model=TaskOut)
def start_task_endpoint(
    task_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Task:
    task = db.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Vazifa topilmadi")
    return start_task(db, task, user)


@router.post("/tasks/{task_id}/review", response_model=TaskOut)
def review_task_endpoint(
    task_id: int,
    data: TaskReview,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
) -> Task:
    task = db.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Vazifa topilmadi")
    return review_task(db, task, data.approved, data.comment)


def _export_data(db: Session, start: date, end: date):
    timezone = ZoneInfo(settings.timezone)
    start_at = datetime.combine(start, time.min, tzinfo=timezone)
    end_at = datetime.combine(date.fromordinal(end.toordinal() + 1), time.min, tzinfo=timezone)
    attendance = db.execute(
        select(Attendance, User, Office)
        .join(User, Attendance.user_id == User.id)
        .join(Office, Attendance.office_id == Office.id)
        .where(Attendance.work_date.between(start, end))
        .order_by(Attendance.work_date, User.full_name)
    ).all()
    tasks = db.execute(
        select(Task, User)
        .join(User, Task.assigned_to == User.id)
        .where(
            or_(
                (Task.created_at >= start_at) & (Task.created_at < end_at),
                (Task.completed_at >= start_at) & (Task.completed_at < end_at),
            )
        )
        .order_by(Task.created_at)
    ).all()
    return attendance, tasks


def _local_time(value: datetime | None, fmt: str) -> str:
    if value is None:
        return ""
    return value.astimezone(ZoneInfo(settings.timezone)).strftime(fmt)


@router.get("/reports.xlsx")
def export_excel(
    date_from: date | None = None,
    date_to: date | None = None,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
) -> StreamingResponse:
    start, end = get_date_range(date_from, date_to)
    attendance, tasks = _export_data(db, start, end)
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Davomat"
    sheet.append(["Sana", "Xodim", "Ofis", "Keldi", "Ketdi", "Holat", "Kechikish (daq.)"])
    for row, user, office in attendance:
        sheet.append([
            row.work_date.isoformat(),
            user.full_name,
            office.name,
            _local_time(row.check_in_time, "%H:%M"),
            _local_time(row.check_out_time, "%H:%M"),
            row.status,
            row.late_minutes,
        ])
    task_sheet = workbook.create_sheet("Vazifalar")
    task_sheet.append(["ID", "Vazifa", "Xodim", "Holat", "Tekshiruv", "Yaratildi"])
    for task, user in tasks:
        task_sheet.append([
            task.id,
            task.title,
            user.full_name,
            task.status,
            task.review_status,
            _local_time(task.created_at, "%Y-%m-%d %H:%M"),
        ])
    for tab in workbook.worksheets:
        tab.freeze_panes = "A2"
        tab.auto_filter.ref = tab.dimensions
        for cell in tab[1]:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="2457A7")
        for column in tab.columns:
            width = min(max(max(len(str(cell.value or "")) for cell in column) + 2, 12), 34)
            tab.column_dimensions[column[0].column_letter].width = width
    output = BytesIO()
    workbook.save(output)
    output.seek(0)
    return StreamingResponse(
        output,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename=smart-hr-{start}-{end}.xlsx"},
    )


@router.get("/reports.pdf")
def export_pdf(
    date_from: date | None = None,
    date_to: date | None = None,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
) -> StreamingResponse:
    start, end = get_date_range(date_from, date_to)
    attendance, tasks = _export_data(db, start, end)
    output = BytesIO()
    document = SimpleDocTemplate(output, pagesize=landscape(A4), title="Smart HR hisoboti")
    styles = _pdf_styles()
    story = [Paragraph(f"Davomat va vazifalar hisoboti: {start} — {end}", styles["Title"]), Spacer(1, 14)]
    attendance_data = [["Sana", "Xodim", "Ofis", "Keldi", "Ketdi", "Holat", "Kechikish"]]
    attendance_data.extend([
        [
            str(row.work_date),
            Paragraph(escape(user.full_name), styles["BodyText"]),
            Paragraph(escape(office.name), styles["BodyText"]),
            _local_time(row.check_in_time, "%H:%M"),
            _local_time(row.check_out_time, "%H:%M") or "—",
            row.status,
            str(row.late_minutes),
        ]
        for row, user, office in attendance
    ])
    story.extend([Paragraph("Davomat", styles["Heading2"]), _pdf_table(attendance_data), Spacer(1, 12)])
    task_data = [["ID", "Vazifa", "Xodim", "Holat", "Tekshiruv"]]
    task_data.extend([
        [
            str(task.id),
            Paragraph(escape(task.title), styles["BodyText"]),
            Paragraph(escape(user.full_name), styles["BodyText"]),
            task.status,
            task.review_status,
        ]
        for task, user in tasks
    ])
    story.extend([Paragraph("Vazifalar", styles["Heading2"]), _pdf_table(task_data)])
    document.build(story)
    output.seek(0)
    return StreamingResponse(
        output,
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename=smart-hr-{start}-{end}.pdf"},
    )


def _pdf_table(rows: list[list]) -> Table:
    table = Table(rows, repeatRows=1, hAlign="LEFT")
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2457a7")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#cbd5e1")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f1f5f9")]),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
    ]))
    return table


def _pdf_styles():
    styles = getSampleStyleSheet()
    font_path = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
    if font_path.exists():
        if "HRUnicode" not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont("HRUnicode", str(font_path)))
        for name in ("Title", "Heading2", "BodyText"):
            styles[name].fontName = "HRUnicode"
    return styles

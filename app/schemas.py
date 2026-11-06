from datetime import date, datetime, time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class LoginInput(BaseModel):
    username: str
    password: str


class TokenOutput(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserCreate(BaseModel):
    full_name: str = Field(min_length=2, max_length=160)
    phone: str | None = Field(default=None, max_length=40)
    role: Literal["admin", "employee"] = "employee"
    telegram_id: int | None = None
    username: str | None = Field(default=None, max_length=80)
    password: str | None = Field(default=None, min_length=8)


class UserUpdate(BaseModel):
    full_name: str | None = Field(default=None, min_length=2, max_length=160)
    phone: str | None = Field(default=None, max_length=40)
    role: Literal["admin", "employee"] | None = None
    is_active: bool | None = None


class UserOut(ORMModel):
    id: int
    telegram_id: int | None
    username: str | None
    full_name: str
    phone: str | None
    role: str
    is_active: bool


class UserCreated(BaseModel):
    user: UserOut
    invite_token: str | None = None
    telegram_deep_link: str | None = None


class OfficeCreate(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    radius_meters: int = Field(default=100, ge=10, le=5000)
    work_start_time: time = time(9, 0)


class OfficeUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=120)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    radius_meters: int | None = Field(default=None, ge=10, le=5000)
    work_start_time: time | None = None
    is_active: bool | None = None


class OfficeOut(ORMModel):
    id: int
    name: str
    latitude: float
    longitude: float
    radius_meters: int
    work_start_time: time
    is_active: bool


class LocationInput(BaseModel):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)


class AttendanceOut(ORMModel):
    id: int
    user_id: int
    office_id: int
    work_date: date
    check_in_time: datetime
    check_out_time: datetime | None
    location_lat: float
    location_lon: float
    checkout_lat: float | None
    checkout_lon: float | None
    status: str
    late_minutes: int


class MonthlyAttendance(BaseModel):
    year: int
    month: int
    days_present: int
    late_days: int
    late_minutes: int


class TaskCreate(BaseModel):
    title: str = Field(min_length=2, max_length=200)
    description: str | None = None
    assigned_to: int
    due_at: datetime | None = None


class TaskSubmit(BaseModel):
    proof_file_id: str | None = Field(default=None, max_length=255)
    proof_comment: str | None = None


class TaskReview(BaseModel):
    approved: bool
    comment: str | None = None


class TaskOut(ORMModel):
    id: int
    title: str
    description: str | None
    assigned_to: int
    created_by: int
    status: str
    review_status: str
    proof_file_id: str | None
    proof_comment: str | None
    review_comment: str | None
    created_at: datetime
    due_at: datetime | None
    completed_at: datetime | None

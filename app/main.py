from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy import select

from app.api import router
from app.config import settings
from app.db import SessionLocal, init_db
from app.models import User
from app.security import hash_password


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    if settings.bootstrap_admin_password:
        with SessionLocal() as db:
            admin = db.scalar(select(User).where(User.username == settings.bootstrap_admin_username))
            if admin is None:
                db.add(User(
                    username=settings.bootstrap_admin_username,
                    full_name=settings.bootstrap_admin_name,
                    telegram_id=settings.bootstrap_admin_telegram_id,
                    role="admin",
                    is_active=True,
                    password_hash=hash_password(settings.bootstrap_admin_password),
                ))
                db.commit()
    yield


app = FastAPI(
    title="Smart HR & Employee Monitoring API",
    description="Davomat, geofencing, vazifalar va HR hisobotlari API-si",
    version="1.0.0",
    lifespan=lifespan,
)
app.include_router(router)


@app.get("/health", tags=["health"])
def health() -> dict[str, str]:
    return {"status": "ok"}

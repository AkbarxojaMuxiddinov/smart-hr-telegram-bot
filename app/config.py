from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    telegram_bot_token: str = ""
    telegram_bot_username: str = ""
    api_base_url: str = "http://api:8000"
    database_url: str = "postgresql+psycopg://hr:hrpassword@localhost:5432/smart_hr"
    redis_url: str = "redis://localhost:6379/0"
    jwt_secret: str = "change-me-before-deploying"
    jwt_expire_minutes: int = 720
    timezone: str = "Asia/Tashkent"
    admin_report_chat_id: int | None = None
    bootstrap_admin_username: str = "admin"
    bootstrap_admin_password: str = ""
    bootstrap_admin_name: str = "HR Admin"
    bootstrap_admin_telegram_id: int | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()

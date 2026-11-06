import httpx

from app.config import settings
from app.models import User
from app.security import create_access_token


class BackendAPIError(Exception):
    """An API request failed or was rejected."""


async def api_request(
    admin: User,
    method: str,
    path: str,
    *,
    json: dict | None = None,
    params: dict | None = None,
) -> dict | list:
    if admin.role != "admin" or not admin.is_active:
        raise BackendAPIError("Admin ruxsati talab qilinadi.")

    token = create_access_token(admin)
    try:
        async with httpx.AsyncClient(base_url=settings.api_base_url.rstrip("/"), timeout=15) as client:
            response = await client.request(
                method,
                path,
                headers={"Authorization": f"Bearer {token}"},
                json=json,
                params=params,
            )
    except httpx.HTTPError as error:
        raise BackendAPIError("API bilan bog‘lanib bo‘lmadi. Server holatini tekshiring.") from error

    try:
        payload = response.json()
    except ValueError:
        payload = {}
    if response.is_error:
        detail = payload.get("detail", "So‘rov bajarilmadi") if isinstance(payload, dict) else "So‘rov bajarilmadi"
        raise BackendAPIError(str(detail))
    return payload

from __future__ import annotations

from urllib.parse import urlsplit

from app.config import settings
from app.errors import DistributionError


def require_authorized_sandbox(destination: dict) -> None:
    if settings.simulation_mode:
        return
    host = (urlsplit(str(destination.get("url", ""))).hostname or "").lower()
    if host in {"localhost", "127.0.0.1", "::1", "lemmy", "ghost"}:
        return
    if destination.get("settings", {}).get("authorized_sandbox") is not True:
        raise DistributionError(
            "CONNECTOR_DISABLED",
            "Live publishing is blocked until this destination is explicitly marked as an authorized sandbox.",
            details={"required_setting": "authorized_sandbox=true"},
            status_code=403,
        )

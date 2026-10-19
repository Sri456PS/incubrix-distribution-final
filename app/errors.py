from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class DistributionError(Exception):
    code: str
    message: str
    retryable: bool = False
    details: dict[str, Any] | None = None
    status_code: int = 400


ERROR_CODES = {
    "DEST_SPEC_VIOLATION": False,
    "DEST_RULE_FREQUENCY_CAP": True,
    "CONNECTION_AUTH_EXPIRED": False,
    "PLATFORM_RATE_LIMIT": True,
    "PLATFORM_QUOTA_EXHAUSTED": True,
    "PLATFORM_CONTENT_REJECTED": False,
    "PLATFORM_UNAVAILABLE": True,
    "CONNECTOR_DISABLED": False,
    "IDEMPOTENCY_CONFLICT": False,
    "DESTINATION_NOT_FOUND": False,
    "CONNECTOR_NOT_FOUND": False,
    "METHOD_NOT_SUPPORTED": False,
    "CERTIFICATION_FAILED": False,
    "INVALID_REQUEST": False,
    "STORAGE_UNAVAILABLE": True,
    "JOB_NOT_FOUND": False,
}

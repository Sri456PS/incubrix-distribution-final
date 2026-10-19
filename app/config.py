from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    app_env: str = os.getenv("APP_ENV", "development")
    simulation_mode: bool = _bool("SIMULATION_MODE", True)
    database_path: str = os.getenv("DATABASE_PATH", "./data/incubrix.db")
    storage_backend: str = os.getenv("STORAGE_BACKEND", "local").lower()
    storage_path: str = os.getenv("STORAGE_PATH", "./data/objects")
    s3_endpoint_url: str = os.getenv("S3_ENDPOINT_URL", "http://localhost:9000")
    s3_bucket: str = os.getenv("S3_BUCKET", "incubrix-results")
    s3_access_key: str = os.getenv("S3_ACCESS_KEY", "")
    s3_secret_key: str = os.getenv("S3_SECRET_KEY", "")
    s3_region: str = os.getenv("S3_REGION", "us-east-1")
    public_base_url: str = os.getenv("PUBLIC_BASE_URL", "http://localhost:8000")
    connector_timeout_seconds: float = float(
        os.getenv("CONNECTOR_TIMEOUT_SECONDS", "10")
    )
    max_rate_limit_wait_seconds: float = float(
        os.getenv("MAX_RATE_LIMIT_WAIT_SECONDS", "30")
    )
    job_poll_seconds: float = float(os.getenv("JOB_POLL_SECONDS", "0.2"))
    max_upload_bytes: int = int(os.getenv("MAX_UPLOAD_BYTES", "25000000"))
    callback_hmac_secret: str = os.getenv("CALLBACK_HMAC_SECRET", "")
    service_api_token: str = os.getenv("SERVICE_API_TOKEN", "")
    api_rate_limit_per_minute: int = int(os.getenv("API_RATE_LIMIT_PER_MINUTE", "120"))
    discourse_base_url: str = os.getenv("DISCOURSE_BASE_URL", "")
    discourse_api_key: str = os.getenv("DISCOURSE_API_KEY", "")
    discourse_api_username: str = os.getenv("DISCOURSE_API_USERNAME", "system")
    lemmy_base_url: str = os.getenv("LEMMY_BASE_URL", "")
    lemmy_jwt: str = os.getenv("LEMMY_JWT", "")
    lemmy_community_id: str = os.getenv("LEMMY_COMMUNITY_ID", "")
    devto_base_url: str = os.getenv("DEVTO_BASE_URL", "https://dev.to/api")
    devto_api_key: str = os.getenv("DEVTO_API_KEY", "")
    ghost_base_url: str = os.getenv("GHOST_BASE_URL", "")
    ghost_admin_api_key: str = os.getenv("GHOST_ADMIN_API_KEY", "")
    webhook_shared_secret: str = os.getenv("WEBHOOK_SHARED_SECRET", "")

    def ensure_directories(self) -> None:
        Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)
        if self.storage_backend == "local":
            Path(self.storage_path).mkdir(parents=True, exist_ok=True)


settings = Settings()

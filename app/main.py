from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import re
import socket
import sqlite3
import threading
import time
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from ipaddress import ip_address
from typing import Annotated, Any
from urllib.parse import urlsplit

import httpx
from fastapi import (
    FastAPI,
    File,
    Header,
    HTTPException,
    Request,
    UploadFile,
)
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.catalogue import list_destinations, normalize_url
from app.config import settings
from app.connectors import CONNECTOR_KEYS, MANIFESTS, dispatch, manifest
from app.database import connect, init_db
from app.errors import ERROR_CODES, DistributionError
from app.jobs import (
    create_job,
    enqueue_due_recertifications,
    recover_queued_jobs,
    run_job,
    store_sync_idempotency,
    utcnow,
)
from app.policy import assess_relevance, decode_destination, tailor
from app.safety import require_authorized_sandbox
from app.storage import storage


class JsonLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        item = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname.lower(),
            "message": record.getMessage(),
            "job_id": getattr(record, "job_id", None),
            "correlation_id": getattr(record, "correlation_id", None),
        }
        return json.dumps(item, ensure_ascii=False)


logger = logging.getLogger("incubrix")
logger.setLevel(logging.INFO)
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonLogFormatter())
    logger.addHandler(handler)

_request_windows: dict[str, list[float]] = {}
_request_windows_lock = threading.Lock()


async def _worker(stop: asyncio.Event) -> None:
    last_recertification_sweep = 0.0
    while not stop.is_set():
        if time.monotonic() - last_recertification_sweep >= 3600:
            await asyncio.to_thread(enqueue_due_recertifications)
            last_recertification_sweep = time.monotonic()
        jobs = await asyncio.to_thread(recover_queued_jobs)
        for job_id in jobs:
            if stop.is_set():
                break
            await asyncio.to_thread(run_job, job_id)
        try:
            await asyncio.wait_for(
                stop.wait(), timeout=max(0.05, settings.job_poll_seconds)
            )
        except TimeoutError:
            pass


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    await asyncio.to_thread(storage.ensure_ready)
    stop = asyncio.Event()
    worker = asyncio.create_task(_worker(stop))
    yield
    stop.set()
    await worker


app = FastAPI(
    title="IncuBrix Long-Tail Distribution",
    version="1.0.0",
    lifespan=lifespan,
    description="Configuration-driven long-tail destination connectors, policy checks, idempotent jobs and certification evidence.",
)
app.openapi_version = "3.1.0"


@app.middleware("http")
async def correlation_and_logging(request: Request, call_next):
    correlation_id = request.headers.get("X-Correlation-Id") or str(uuid.uuid4())
    request.state.correlation_id = correlation_id
    if request.url.path not in {
        "/health",
        "/ready",
        "/openapi.json",
        "/docs",
        "/redoc",
    }:
        if settings.service_api_token:
            supplied = request.headers.get("Authorization", "")
            expected = f"Bearer {settings.service_api_token}"
            if not hmac.compare_digest(supplied, expected):
                return JSONResponse(
                    status_code=401,
                    content={
                        "error": {
                            "code": "CONNECTION_AUTH_EXPIRED",
                            "message": "A valid service bearer token is required.",
                            "retryable": False,
                            "details": {},
                        }
                    },
                    headers={"X-Correlation-Id": correlation_id},
                )
        limit = max(1, settings.api_rate_limit_per_minute)
        client_ip = request.client.host if request.client else "unknown"
        now = time.monotonic()
        with _request_windows_lock:
            recent = [
                stamp
                for stamp in _request_windows.get(client_ip, [])
                if now - stamp < 60
            ]
            if len(recent) >= limit:
                retry_after = max(1, int(60 - (now - recent[0])))
                return JSONResponse(
                    status_code=429,
                    content={
                        "error": {
                            "code": "PLATFORM_RATE_LIMIT",
                            "message": "Service request rate limit reached.",
                            "retryable": True,
                            "details": {"retry_after": retry_after},
                        }
                    },
                    headers={
                        "Retry-After": str(retry_after),
                        "X-Correlation-Id": correlation_id,
                    },
                )
            recent.append(now)
            _request_windows[client_ip] = recent
    try:
        response = await call_next(request)
    except Exception as exc:
        logger.error(
            "request.failed",
            extra={
                "correlation_id": correlation_id,
                "job_id": request.headers.get("X-Job-Id"),
                "error_type": type(exc).__name__,
            },
        )
        raise
    response.headers["X-Correlation-Id"] = correlation_id
    logger.info(
        "request.complete",
        extra={
            "correlation_id": correlation_id,
            "job_id": request.headers.get("X-Job-Id"),
        },
    )
    return response


@app.exception_handler(DistributionError)
async def distribution_error_handler(_: Request, exc: DistributionError):
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": {
                "code": exc.code,
                "message": exc.message,
                "retryable": exc.retryable,
                "details": exc.details or {},
            }
        },
    )


@app.exception_handler(HTTPException)
async def http_error_handler(_: Request, exc: HTTPException):
    detail = exc.detail
    if isinstance(detail, dict) and "error" in detail:
        body = detail
    else:
        body = {
            "error": {
                "code": "INVALID_REQUEST",
                "message": str(detail),
                "retryable": False,
                "details": {},
            }
        }
    return JSONResponse(status_code=exc.status_code, content=body, headers=exc.headers)


class DestinationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)
    url: str
    platform_key: str
    surface: str
    category: str = "community"
    audience_tags: list[str] = Field(default_factory=list)
    language: str = "en"
    geo: str | None = None
    est_audience: int | None = Field(default=None, ge=0)
    rules_url: str | None = None
    frequency_cap: int = Field(default=0, ge=0)
    frequency_window_seconds: int = Field(default=604800, ge=60)
    self_promo_allowed: bool = True
    link_policy: str = "allowed"
    disclosure_required: bool = False
    automation_allowed: bool = True
    connector_key: str
    relevance_score: int | None = Field(default=None, ge=1, le=5)
    relevance_reason: str | None = None
    settings: dict[str, Any] = Field(default_factory=dict)
    rules_confirmed: bool = True

    @field_validator("url")
    @classmethod
    def valid_url(cls, value: str) -> str:
        return normalize_url(value)

    @field_validator("settings")
    @classmethod
    def no_secrets(cls, value: dict[str, Any]) -> dict[str, Any]:
        for key, item in value.items():
            if re.search(
                r"(secret|token|password|api[_-]?key|credential)", key, re.IGNORECASE
            ):
                raise ValueError(
                    "Secrets and credentials belong in environment variables, not destination settings."
                )
            if isinstance(item, str) and re.search(
                r"(bearer\s+|sk-[A-Za-z0-9]{16,})", item, re.IGNORECASE
            ):
                raise ValueError(
                    "Credential-like values are not allowed in destination settings."
                )
        return value


class Rendition(BaseModel):
    model_config = ConfigDict(extra="allow")
    title: str = ""
    body: str = ""
    media_type: str = "text"
    canonical_url: str | None = None
    is_promotional: bool = False
    ai_generated: bool = False
    ai_disclosure: str | None = None


class PublicationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    destination_id: str
    rendition: Rendition
    callback_url: str | None = None


class CertificationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    destination_id: str
    test_rendition: Rendition = Field(
        default_factory=lambda: Rendition(
            title="Certification test", body="Temporary local certification content."
        )
    )
    callback_url: str | None = None


class RuleProposalInput(BaseModel):
    rules_text: str = Field(min_length=1, max_length=20000)
    source_url: str | None = None


class RecommendationInput(BaseModel):
    content_tags: list[str] = Field(default_factory=list)
    category: str = "article"
    language: str = "en"
    top_k: int = Field(default=10, ge=1, le=100)


class RecommendationCase(BaseModel):
    content_tags: list[str] = Field(default_factory=list)
    language: str = "en"
    relevant_destination_ids: list[str]


class RecommendationEvaluationInput(BaseModel):
    cases: list[RecommendationCase] = Field(min_length=1, max_length=1000)
    top_k: int = Field(default=10, ge=1, le=10)


def _require_idem(value: str | None) -> str:
    if not value or len(value) > 200:
        raise DistributionError(
            "INVALID_REQUEST",
            "Every POST requires a non-empty Idempotency-Key header (maximum 200 characters).",
            status_code=400,
        )
    return value


def _public_url(url: str) -> bool:
    parsed = urlsplit(url)
    if (
        parsed.scheme not in {"https", "http"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        return False
    try:
        addresses = (
            {ip_address(parsed.hostname)}
            if re.fullmatch(r"[0-9.]+", parsed.hostname) or ":" in parsed.hostname
            else {
                ip_address(item[4][0])
                for item in socket.getaddrinfo(
                    parsed.hostname,
                    parsed.port or (443 if parsed.scheme == "https" else 80),
                    type=socket.SOCK_STREAM,
                )
            }
        )
    except (OSError, ValueError):
        return False
    return all(address.is_global for address in addresses)


def _destination_input_record(data: DestinationInput) -> dict[str, Any]:
    raw = data.model_dump()
    score, reason = assess_relevance(
        raw["audience_tags"], raw["category"], raw["language"]
    )
    raw["relevance_score"] = raw["relevance_score"] or score
    raw["relevance_reason"] = raw["relevance_reason"] or reason
    raw["connector_key"] = raw["connector_key"].lower()
    if raw["connector_key"] not in CONNECTOR_KEYS:
        raise DistributionError(
            "CONNECTOR_NOT_FOUND",
            "connector_key must be one of the six supported connectors.",
            details={"supported": list(CONNECTOR_KEYS)},
            status_code=422,
        )
    connector_surface_keys = {
        surface["key"] for surface in manifest(raw["connector_key"])["surfaces"]
    }
    if raw["surface"] not in connector_surface_keys:
        raise DistributionError(
            "INVALID_REQUEST",
            "surface must be one of the selected connector's manifest surfaces.",
            details={
                "connector_key": raw["connector_key"],
                "supported": sorted(connector_surface_keys),
            },
            status_code=422,
        )
    return raw


def _insert_destination(data: DestinationInput) -> dict[str, Any]:
    item = _destination_input_record(data)
    destination_id, now = str(uuid.uuid4()), utcnow()
    with connect() as db:
        db.execute(
            """INSERT INTO destinations (id,name,url,platform_key,surface,category,audience_tags,language,geo,est_audience,rules_url,
          frequency_cap,frequency_window_seconds,self_promo_allowed,link_policy,disclosure_required,automation_allowed,connector_key,
          relevance_score,relevance_reason,level,last_certified_at,enabled,settings_json,rules_confirmed,created_at)
          VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'L0',NULL,1,?,?,?)""",
            (
                destination_id,
                item["name"],
                item["url"],
                item["platform_key"],
                item["surface"],
                item["category"],
                json.dumps(item["audience_tags"]),
                item["language"],
                item["geo"],
                item["est_audience"],
                item["rules_url"],
                item["frequency_cap"],
                item["frequency_window_seconds"],
                int(item["self_promo_allowed"]),
                item["link_policy"],
                int(item["disclosure_required"]),
                int(item["automation_allowed"]),
                item["connector_key"],
                item["relevance_score"],
                item["relevance_reason"],
                json.dumps(item["settings"]),
                int(item["rules_confirmed"]),
                now,
            ),
        )
        row = db.execute(
            "SELECT * FROM destinations WHERE id=?", (destination_id,)
        ).fetchone()
    return decode_destination(row)


def _job_view(row: dict[str, Any]) -> dict[str, Any]:
    result = json.loads(row["result_json"]) if row.get("result_json") else None
    error = json.loads(row["error_json"]) if row.get("error_json") else None
    return {
        "job_id": row["id"],
        "status": row["status"],
        "progress": row["progress"],
        "result_links": {
            "result": result.get("result_url"),
            "manifest": result.get("manifest_url"),
        }
        if result
        else {},
        "error": error,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _instant_manifest(
    result: dict[str, Any], provider: str, started: float, stable_key: str
) -> dict[str, Any]:
    manifest_data = {
        "schema_version": "1.0",
        "provider": provider,
        "provider_version": "1.0.0",
        "models": [],
        "settings": {
            "simulation_mode": settings.simulation_mode,
            "storage_backend": settings.storage_backend,
        },
        "seed": None,
        "timings_ms": {"total": round((time.monotonic() - started) * 1000, 2)},
        "cost_estimate": {
            "amount": 0,
            "currency": "USD",
            "basis": "No paid model or provider calls are made.",
        },
        "warnings": [],
        "rights": {
            "content_owner": "caller",
            "media_license": "caller-supplied; validate before publishing",
            "ai_generated_media": False,
        },
        "result": result,
    }
    manifest_id = hashlib.sha256(stable_key.encode()).hexdigest()
    url = storage.put(
        f"manifests/{manifest_id}.json",
        json.dumps(manifest_data, ensure_ascii=False, indent=2).encode(),
        "application/json",
    )
    return {**result, "manifest_url": url, "manifest": manifest_data}


def _queue(
    kind: str,
    body: dict[str, Any],
    key: str,
    callback_url: str | None,
    correlation_id: str | None = None,
) -> tuple[dict[str, Any], bool]:
    if callback_url and (
        not _public_url(callback_url) or not settings.callback_hmac_secret
    ):
        raise DistributionError(
            "INVALID_REQUEST",
            "Callbacks require a public HTTP(S) URL and CALLBACK_HMAC_SECRET.",
            status_code=422,
        )
    job, created = create_job(kind, body, key, callback_url, correlation_id)
    logger.info(
        "job.queued", extra={"job_id": job["id"], "correlation_id": correlation_id}
    )
    return job, created


@app.get("/health", tags=["health"])
def health():
    return {"status": "ok"}


@app.get("/ready", tags=["health"])
def ready():
    checks = {"database": False, "storage": False}
    try:
        with connect() as db:
            db.execute("SELECT 1").fetchone()
        checks["database"] = True
    except (sqlite3.Error, OSError) as exc:
        logger.warning(
            "readiness.database_failed",
            extra={
                "correlation_id": None,
                "job_id": None,
                "error_type": type(exc).__name__,
            },
        )
    try:
        storage.ensure_ready()
        checks["storage"] = True
    except DistributionError:
        pass
    return JSONResponse(
        status_code=200 if all(checks.values()) else 503,
        content={
            "status": "ready" if all(checks.values()) else "not_ready",
            "checks": checks,
        },
    )


@app.get("/api/v1/connectors", tags=["connectors"])
def list_connectors():
    return {"connectors": [MANIFESTS[k] for k in CONNECTOR_KEYS]}


@app.get("/api/v1/connectors/{connector_key}/manifest", tags=["connectors"])
def get_manifest(connector_key: str):
    return manifest(connector_key)


@app.post("/connector/v1/{connector_key}/{method}", tags=["connector contract"])
def connector_method(
    connector_key: str,
    method: str,
    payload: dict[str, Any],
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    key = _require_idem(idempotency_key)
    if method == "publish":
        if not settings.simulation_mode:
            require_authorized_sandbox(payload.get("destination", {}))
        payload["idempotency_key"] = key
    return store_sync_idempotency(
        f"connector:{connector_key}:{method}",
        key,
        payload,
        lambda: dispatch(connector_key, method, payload),
    )


@app.post("/api/v1/destinations", status_code=201, tags=["catalogue"])
def create_destination(
    body: DestinationInput,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    key = _require_idem(idempotency_key)
    return store_sync_idempotency(
        "destination:create", key, body.model_dump(), lambda: _insert_destination(body)
    )


@app.get("/api/v1/destinations", tags=["catalogue"])
def get_destinations():
    return {"destinations": list_destinations()}


@app.get("/api/v1/destinations/{destination_id}", tags=["catalogue"])
def get_destination(destination_id: str):
    with connect() as db:
        row = db.execute(
            "SELECT * FROM destinations WHERE id=?", (destination_id,)
        ).fetchone()
    if not row:
        raise DistributionError(
            "DESTINATION_NOT_FOUND", "Destination was not found.", status_code=404
        )
    return decode_destination(row)


@app.post("/api/v1/destinations/import", status_code=202, tags=["catalogue"])
async def import_destinations(
    request: Request,
    file: Annotated[UploadFile, File()],
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    key = _require_idem(idempotency_key)
    content = await file.read(settings.max_upload_bytes + 1)
    if len(content) > settings.max_upload_bytes:
        raise DistributionError(
            "INVALID_REQUEST", "CSV exceeds MAX_UPLOAD_BYTES.", status_code=413
        )
    try:
        decoded = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise DistributionError(
            "INVALID_REQUEST", "CSV must be UTF-8 encoded.", status_code=422
        ) from exc
    job, _ = _queue(
        "catalogue_import",
        {"csv_utf8": decoded},
        key,
        None,
        request.state.correlation_id,
    )
    return JSONResponse(
        status_code=202,
        content={
            "job_id": job["id"],
            "status": job["status"],
            "status_url": f"/api/v1/jobs/{job['id']}",
        },
    )


@app.post("/api/v1/publications", status_code=202, tags=["publishing"])
def publish(
    request: Request,
    body: PublicationInput,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    key = _require_idem(idempotency_key)
    job, _ = _queue(
        "publish",
        {
            "destination_id": body.destination_id,
            "rendition": body.rendition.model_dump(),
        },
        key,
        body.callback_url,
        request.state.correlation_id,
    )
    return JSONResponse(
        status_code=202,
        content={
            "job_id": job["id"],
            "status": job["status"],
            "status_url": f"/api/v1/jobs/{job['id']}",
        },
    )


@app.post("/api/v1/certifications", status_code=202, tags=["certification"])
def certify(
    request: Request,
    body: CertificationInput,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    key = _require_idem(idempotency_key)
    job, _ = _queue(
        "certification",
        {
            "destination_id": body.destination_id,
            "test_rendition": body.test_rendition.model_dump(),
        },
        key,
        body.callback_url,
        request.state.correlation_id,
    )
    return JSONResponse(
        status_code=202,
        content={
            "job_id": job["id"],
            "status": job["status"],
            "status_url": f"/api/v1/jobs/{job['id']}",
        },
    )


@app.get("/api/v1/jobs/{job_id}", tags=["jobs"])
def get_job(job_id: str):
    with connect() as db:
        row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        raise DistributionError("JOB_NOT_FOUND", "Job was not found.", status_code=404)
    return _job_view(dict(row))


@app.get("/api/v1/jobs/{job_id}/result", tags=["jobs"])
def get_job_result(job_id: str):
    with connect() as db:
        row = db.execute(
            "SELECT result_json,status FROM jobs WHERE id=?", (job_id,)
        ).fetchone()
    if not row:
        raise DistributionError("JOB_NOT_FOUND", "Job was not found.", status_code=404)
    if not row["result_json"]:
        raise DistributionError(
            "PLATFORM_UNAVAILABLE",
            "Job has no stored result yet.",
            True,
            {"status": row["status"]},
            409,
        )
    return json.loads(row["result_json"])


@app.get("/api/v1/certifications/{certification_id}", tags=["certification"])
def get_certification(certification_id: str):
    with connect() as db:
        row = db.execute(
            "SELECT * FROM certifications WHERE id=?", (certification_id,)
        ).fetchone()
    if not row:
        raise DistributionError(
            "DESTINATION_NOT_FOUND",
            "Certification record was not found.",
            status_code=404,
        )
    return {**dict(row), "evidence": json.loads(row["evidence_json"])}


@app.get("/api/v1/files/{key:path}", tags=["storage"])
def get_file(key: str):
    data, content_type = storage.get(key)
    return Response(
        content=data,
        media_type=content_type,
        headers={"Content-Disposition": "attachment"},
    )


@app.post("/api/v1/assets", status_code=201, tags=["storage"])
async def upload_asset(
    file: Annotated[UploadFile, File()],
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    key = _require_idem(idempotency_key)
    data = await file.read(settings.max_upload_bytes + 1)
    if len(data) > settings.max_upload_bytes:
        raise DistributionError(
            "INVALID_REQUEST", "Upload exceeds MAX_UPLOAD_BYTES.", status_code=413
        )
    digest = hashlib.sha256(data).hexdigest()
    started = time.monotonic()
    result = store_sync_idempotency(
        "asset:upload",
        key,
        {"sha256": digest, "filename": file.filename},
        lambda: _instant_manifest(
            {
                "file_url": storage.put(
                    f"assets/{digest}/{file.filename or 'upload.bin'}",
                    data,
                    file.content_type or "application/octet-stream",
                ),
                "sha256": digest,
            },
            "object-storage",
            started,
            "asset:upload:" + key,
        ),
    )
    return result


class UrlInput(BaseModel):
    url: str


@app.post("/api/v1/assets/from-url", status_code=201, tags=["storage"])
def fetch_asset(
    body: UrlInput,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    key = _require_idem(idempotency_key)
    if not _public_url(body.url):
        raise DistributionError(
            "INVALID_REQUEST",
            "URL input must resolve only to public HTTP(S) addresses.",
            status_code=422,
        )

    started = time.monotonic()

    def do_fetch():
        try:
            response = httpx.get(
                body.url,
                follow_redirects=False,
                timeout=settings.connector_timeout_seconds,
            )
        except httpx.HTTPError as exc:
            raise DistributionError(
                "PLATFORM_UNAVAILABLE",
                "Input URL could not be fetched.",
                True,
                status_code=502,
            ) from exc
        if response.status_code >= 400:
            raise DistributionError(
                "PLATFORM_UNAVAILABLE",
                "Input URL returned an error.",
                True,
                {"status": response.status_code},
                502,
            )
        if len(response.content) > settings.max_upload_bytes:
            raise DistributionError(
                "INVALID_REQUEST",
                "Remote input exceeds MAX_UPLOAD_BYTES.",
                status_code=413,
            )
        digest = hashlib.sha256(response.content).hexdigest()
        content_type = response.headers.get(
            "content-type", "application/octet-stream"
        ).split(";", 1)[0]
        return _instant_manifest(
            {
                "file_url": storage.put(
                    f"assets/{digest}/remote.bin", response.content, content_type
                ),
                "sha256": digest,
            },
            "object-storage",
            started,
            "asset:url:" + key,
        )

    return store_sync_idempotency("asset:url", key, body.model_dump(), do_fetch)


@app.get("/api/v1/simulation/posts/{platform_post_id}", tags=["simulation"])
def simulated_post(platform_post_id: str):
    with connect() as db:
        row = db.execute(
            "SELECT * FROM posts WHERE platform_post_id=? AND simulation=1",
            (platform_post_id,),
        ).fetchone()
    if not row:
        raise DistributionError(
            "DESTINATION_NOT_FOUND", "Simulated post was not found.", status_code=404
        )
    return {
        "platform_post_id": row["platform_post_id"],
        "title": row["title"],
        "body": row["body"],
        "status": row["status"],
        "created_at": row["created_at"],
    }


@app.post("/api/v1/destinations/{destination_id}/tailor", tags=["catalogue"])
def tailor_for_destination(
    destination_id: str,
    rendition: Rendition,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    key = _require_idem(idempotency_key)
    destination_response = get_destination(destination_id)
    started = time.monotonic()
    result = store_sync_idempotency(
        "tailor:" + destination_id,
        key,
        rendition.model_dump(),
        lambda: _instant_manifest(
            tailor(
                rendition.model_dump(),
                destination_response,
                manifest(destination_response["connector_key"]),
            ),
            destination_response["connector_key"],
            started,
            "tailor:" + destination_id + ":" + key,
        ),
    )
    return result


@app.post("/api/v1/recommendations", tags=["recommendations"])
def recommend(
    body: RecommendationInput,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    key = _require_idem(idempotency_key)
    request_data = body.model_dump()
    started = time.monotonic()

    def calculate():
        return _instant_manifest(
            {
                "recommendations": _rank_destinations(
                    body.content_tags, body.language, body.top_k
                ),
                "ranking_features": [
                    "audience fit",
                    "posting rules confirmation",
                    "historical successful posts",
                ],
            },
            "local-ranker",
            started,
            "recommendations:" + key,
        )

    return store_sync_idempotency("recommendations", key, request_data, calculate)


def _rank_destinations(
    content_tags: list[str], language: str, top_k: int
) -> list[dict[str, Any]]:
    scored = []
    tags = {tag.lower() for tag in content_tags}
    for destination in list_destinations():
        if not destination["enabled"] or not destination["rules_confirmed"]:
            continue
        if destination["language"] != language:
            continue
        overlap = sorted(
            tags.intersection(tag.lower() for tag in destination["audience_tags"])
        )
        base = destination["relevance_score"]
        with connect() as db:
            records = db.execute(
                "SELECT status FROM posts WHERE destination_id=? AND title NOT LIKE 'IncuBrix certification test %'",
                (destination["id"],),
            ).fetchall()
        total = len(records)
        success = sum(
            1 for record in records if record["status"] in {"live", "deleted"}
        )
        performance = success / total if total else 0.5
        score = min(
            5.0,
            round(
                base * 0.75 + (2 * performance) * 0.25 + min(0.5, 0.1 * len(overlap)),
                2,
            ),
        )
        reason = f"Audience fit {base}/5; matched tags={', '.join(overlap) or 'none'}; historical successful posts={success}/{total}."
        scored.append({"destination": destination, "score": score, "reason": reason})
    ranked = sorted(
        scored, key=lambda item: (-item["score"], item["destination"]["name"])
    )
    return ranked[:top_k]


@app.post("/api/v1/recommendations/evaluate", tags=["recommendations"])
def evaluate_recommendations(
    body: RecommendationEvaluationInput,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    key = _require_idem(idempotency_key)
    started = time.monotonic()

    def evaluate():
        cases = []
        hits = 0
        total_slots = 0
        for case in body.cases:
            ranked = _rank_destinations(case.content_tags, case.language, body.top_k)
            predicted = [entry["destination"]["id"] for entry in ranked]
            relevant = set(case.relevant_destination_ids)
            case_hits = sum(destination_id in relevant for destination_id in predicted)
            hits += case_hits
            total_slots += min(body.top_k, len(predicted))
            cases.append(
                {
                    "predicted_top_k": predicted,
                    "relevant": sorted(relevant),
                    "hits": case_hits,
                }
            )
        precision = hits / total_slots if total_slots else 0.0
        result = {
            "cases": len(cases),
            "top_k": body.top_k,
            "hits": hits,
            "precision_at_k": precision,
            "labeled_set_supplied_by_user": True,
            "case_results": cases,
        }
        report_id = hashlib.sha256(
            ("recommendation-evaluation:" + key).encode()
        ).hexdigest()
        result["evaluation_url"] = storage.put(
            f"recommendations/evaluations/{report_id}.json",
            json.dumps(result, ensure_ascii=False, indent=2).encode(),
            "application/json",
        )
        return _instant_manifest(
            result, "local-ranker", started, "recommendation-evaluation:" + key
        )

    return store_sync_idempotency(
        "recommendation-evaluation", key, body.model_dump(), evaluate
    )


@app.post("/api/v1/destinations/{destination_id}/rules/propose", tags=["catalogue"])
def propose_rules(
    destination_id: str,
    body: RuleProposalInput,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    key = _require_idem(idempotency_key)
    get_destination(destination_id)
    started = time.monotonic()
    # Deterministic, transparent extraction proposal. It is never activated until a human confirms it.
    text = body.rules_text.lower()
    proposal = {
        "self_promo_allowed": not any(
            term in text
            for term in ("no self-promotion", "no promotion", "no promotional")
        ),
        "link_policy": "disallowed"
        if any(term in text for term in ("no links", "links are not allowed"))
        else "allowed",
        "disclosure_required": "ai-generated" in text or "ai generated" in text,
        "automation_allowed": not any(
            term in text
            for term in (
                "manual posting only",
                "no automated",
                "automation is prohibited",
            )
        ),
        "frequency_cap": 1 if "once per week" in text or "weekly" in text else 0,
        "source_url": body.source_url,
        "human_confirmation_required": True,
        "method": "deterministic rule phrase extraction; no AI model used",
    }
    result = store_sync_idempotency(
        "rule-proposal:" + destination_id,
        key,
        body.model_dump(),
        lambda: _instant_manifest(
            proposal,
            "deterministic-rule-extractor",
            started,
            "rule-proposal:" + destination_id + ":" + key,
        ),
    )
    with connect() as db:
        db.execute(
            "UPDATE destinations SET rules_confirmed=0 WHERE id=?", (destination_id,)
        )
    return result


class RuleConfirmInput(BaseModel):
    rules: dict[str, Any]


@app.post("/api/v1/destinations/{destination_id}/rules/confirm", tags=["catalogue"])
def confirm_rules(
    destination_id: str,
    body: RuleConfirmInput,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    key = _require_idem(idempotency_key)
    allowed = {
        "frequency_cap",
        "frequency_window_seconds",
        "self_promo_allowed",
        "link_policy",
        "disclosure_required",
        "automation_allowed",
    }
    if set(body.rules) - allowed:
        raise DistributionError(
            "INVALID_REQUEST",
            "Only destination rule fields can be confirmed.",
            details={"allowed": sorted(allowed)},
            status_code=422,
        )
    boolean_rules = {
        "self_promo_allowed",
        "disclosure_required",
        "automation_allowed",
    }
    for name, value in body.rules.items():
        if name in boolean_rules and not isinstance(value, bool):
            raise DistributionError(
                "INVALID_REQUEST", f"{name} must be a boolean.", status_code=422
            )
        if name == "frequency_cap" and (not isinstance(value, int) or value < 0):
            raise DistributionError(
                "INVALID_REQUEST",
                "frequency_cap must be a non-negative integer.",
                status_code=422,
            )
        if name == "frequency_window_seconds" and (
            not isinstance(value, int) or value < 60
        ):
            raise DistributionError(
                "INVALID_REQUEST",
                "frequency_window_seconds must be at least 60.",
                status_code=422,
            )
        if name == "link_policy" and value not in {
            "allowed",
            "none",
            "no_links",
            "disallowed",
            "canonical_only",
        }:
            raise DistributionError(
                "INVALID_REQUEST", "Unsupported link_policy value.", status_code=422
            )

    def apply_rules():
        with connect() as db:
            if not db.execute(
                "SELECT id FROM destinations WHERE id=?", (destination_id,)
            ).fetchone():
                raise DistributionError(
                    "DESTINATION_NOT_FOUND",
                    "Destination was not found.",
                    status_code=404,
                )
            assignments = ",".join(f"{name}=?" for name in body.rules)
            values = [
                int(value) if isinstance(value, bool) else value
                for value in body.rules.values()
            ]
            db.execute(
                f"UPDATE destinations SET {assignments},rules_confirmed=1 WHERE id=?"
                if assignments
                else "UPDATE destinations SET rules_confirmed=1 WHERE id=?",
                (*values, destination_id) if assignments else (destination_id,),
            )
        return {
            "destination_id": destination_id,
            "rules_confirmed": True,
            "rules": body.rules,
        }

    return store_sync_idempotency(
        "rule-confirm:" + destination_id, key, body.model_dump(), apply_rules
    )


@app.post("/api/v1/destinations/{destination_id}/enable", tags=["catalogue"])
def enable_destination(
    destination_id: str,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    key = _require_idem(idempotency_key)

    def apply_enable():
        destination = get_destination(destination_id)
        if not destination["rules_confirmed"]:
            raise DistributionError(
                "CONNECTOR_DISABLED",
                "Confirm the destination rules before enabling it.",
                status_code=409,
            )
        with connect() as db:
            db.execute(
                "UPDATE destinations SET enabled=1 WHERE id=?", (destination_id,)
            )
        return {"destination_id": destination_id, "enabled": True, "level": "L0"}

    return store_sync_idempotency(
        "destination:enable:" + destination_id,
        key,
        {"destination_id": destination_id},
        apply_enable,
    )


@app.get("/api/v1/errors", tags=["metadata"])
def error_codes():
    return {
        "error_codes": [
            {"code": code, "retryable": retry} for code, retry in ERROR_CODES.items()
        ]
    }


def openapi_document() -> dict[str, Any]:
    return app.openapi()

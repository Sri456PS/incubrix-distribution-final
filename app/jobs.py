from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from app.catalogue import import_csv
from app.config import settings
from app.connectors import CONNECTOR_KEYS, dispatch, manifest
from app.database import connect
from app.errors import DistributionError
from app.policy import decode_destination, tailor
from app.safety import require_authorized_sandbox
from app.storage import storage


def utcnow() -> str:
    return datetime.now(UTC).isoformat()


def request_digest(body: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            body, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
    ).hexdigest()


def create_job(
    kind: str,
    body: dict[str, Any],
    idempotency_key: str,
    callback_url: str | None = None,
    correlation_id: str | None = None,
) -> tuple[dict[str, Any], bool]:
    scoped_key = f"{kind}:{idempotency_key}"
    digest = request_digest(body)
    job_id, now = str(uuid.uuid4()), utcnow()
    with connect() as db:
        prior = db.execute(
            "SELECT * FROM idempotency WHERE key=?", (scoped_key,)
        ).fetchone()
        if prior:
            if prior["request_hash"] != digest:
                raise DistributionError(
                    "IDEMPOTENCY_CONFLICT",
                    "Idempotency-Key was already used with a different request.",
                    status_code=409,
                )
            existing = db.execute(
                "SELECT * FROM jobs WHERE id=?", (prior["job_id"],)
            ).fetchone()
            if existing:
                return dict(existing), False
        db.execute(
            "INSERT INTO jobs (id,kind,status,progress,request_json,callback_url,correlation_id,created_at,updated_at) VALUES (?,?, 'queued',0,?,?,?,?,?)",
            (job_id, kind, json.dumps(body), callback_url, correlation_id, now, now),
        )
        db.execute(
            "INSERT INTO idempotency (key,request_hash,job_id,created_at) VALUES (?,?,?,?)",
            (scoped_key, digest, job_id, now),
        )
        return dict(
            db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        ), True


def store_sync_idempotency(scope: str, key: str, body: Any, fn) -> Any:
    scoped_key, digest = f"sync:{scope}:{key}", request_digest(body)
    with connect() as db:
        prior = db.execute(
            "SELECT * FROM idempotency WHERE key=?", (scoped_key,)
        ).fetchone()
        if prior:
            if prior["request_hash"] != digest:
                raise DistributionError(
                    "IDEMPOTENCY_CONFLICT",
                    "Idempotency-Key was already used with a different request.",
                    status_code=409,
                )
            if prior["response_json"]:
                cached = json.loads(prior["response_json"])
                if "__error__" in cached:
                    err = cached["__error__"]
                    raise DistributionError(
                        err["code"],
                        err["message"],
                        err["retryable"],
                        err["details"],
                        cached["status_code"],
                    )
                return cached
            raise DistributionError(
                "PLATFORM_UNAVAILABLE",
                "An earlier attempt with this key has an unknown outcome; it was not repeated to prevent duplicates.",
                True,
                {"idempotency_key": key},
                503,
            )
        db.execute(
            "INSERT INTO idempotency (key,request_hash,created_at) VALUES (?,?,?)",
            (scoped_key, digest, utcnow()),
        )
    try:
        result = fn()
    except DistributionError as exc:
        cached_error = {
            "__error__": {
                "code": exc.code,
                "message": exc.message,
                "retryable": exc.retryable,
                "details": exc.details or {},
            },
            "status_code": exc.status_code,
        }
        with connect() as db:
            db.execute(
                "UPDATE idempotency SET response_json=? WHERE key=?",
                (json.dumps(cached_error), scoped_key),
            )
        raise
    with connect() as db:
        db.execute(
            "UPDATE idempotency SET response_json=? WHERE key=?",
            (json.dumps(result), scoped_key),
        )
    return result


def _audit(
    job_id: str, correlation_id: str | None, kind: str, details: dict[str, Any]
) -> None:
    with connect() as db:
        db.execute(
            "INSERT INTO audit_events (id,job_id,correlation_id,event_type,details_json,created_at) VALUES (?,?,?,?,?,?)",
            (
                str(uuid.uuid4()),
                job_id,
                correlation_id,
                kind,
                json.dumps(details, ensure_ascii=False),
                utcnow(),
            ),
        )


def _destination(destination_id: str) -> dict[str, Any]:
    with connect() as db:
        row = db.execute(
            "SELECT * FROM destinations WHERE id=?", (destination_id,)
        ).fetchone()
    if not row:
        raise DistributionError(
            "DESTINATION_NOT_FOUND", "Destination was not found.", status_code=404
        )
    return decode_destination(row)


def _manifest(
    job: dict[str, Any], started: float, warnings: list[str] | None = None
) -> dict[str, Any]:
    body = json.loads(job["request_json"])
    key = body.get("connector_key")
    if not key and body.get("destination_id"):
        try:
            key = _destination(body["destination_id"])["connector_key"]
        except DistributionError:
            key = None
    return {
        "schema_version": "1.0",
        "job_id": job["id"],
        "provider": key or "catalogue",
        "provider_version": manifest(key)["version"]
        if key in CONNECTOR_KEYS
        else "1.0.0",
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
            "basis": "No paid AI/provider calls are made by this service.",
        },
        "warnings": warnings or [],
        "rights": {
            "content_owner": "caller",
            "media_license": "caller-supplied; validate before publishing",
            "ai_generated_media": bool(
                body.get("rendition", {}).get("ai_generated_media", False)
            ),
        },
    }


def _live_destination_guard(destination: dict[str, Any]) -> None:
    require_authorized_sandbox(destination)


def _save_result(
    job: dict[str, Any], result: dict[str, Any], manifest_data: dict[str, Any]
) -> dict[str, Any]:
    base = f"jobs/{job['id']}"
    manifest_data["result"] = result
    manifest_url = storage.put(
        f"{base}/manifest.json",
        json.dumps(manifest_data, ensure_ascii=False, indent=2).encode(),
        "application/json",
    )
    result_url = storage.put(
        f"{base}/result.json",
        json.dumps(result, ensure_ascii=False, indent=2).encode(),
        "application/json",
    )
    return {
        **result,
        "result_url": result_url,
        "manifest_url": manifest_url,
        "manifest": manifest_data,
    }


def _run_publication(job: dict[str, Any]) -> dict[str, Any]:
    started = time.monotonic()
    body = json.loads(job["request_json"])
    destination = _destination(body["destination_id"])
    key = destination["connector_key"]
    if key not in CONNECTOR_KEYS:
        raise DistributionError(
            "CONNECTOR_NOT_FOUND", f"No connector '{key}'.", status_code=404
        )
    _live_destination_guard(destination)
    rendition = tailor(body["rendition"], destination, manifest(key))
    result = (
        dispatch(
            key,
            "build_assist_pack",
            {"rendition": rendition, "destination": destination},
        )
        if key == "assisted"
        else dispatch(
            key,
            "publish",
            {
                "rendition": rendition,
                "destination": destination,
                "idempotency_key": job["id"],
            },
            simulation=settings.simulation_mode,
        )
    )
    if key == "assisted":
        caption_url = storage.put(
            f"jobs/{job['id']}/caption.txt",
            result["caption"].encode(),
            "text/plain; charset=utf-8",
        )
        checklist_url = storage.put(
            f"jobs/{job['id']}/checklist.txt",
            "\n".join(result["checklist"]).encode(),
            "text/plain; charset=utf-8",
        )
        result["files"] = [caption_url, checklist_url]
    manifest_data = _manifest(
        job,
        started,
        ["simulation mode: no community received a real post"]
        if settings.simulation_mode
        else [],
    )
    return _save_result(job, result, manifest_data)


def _run_certification(job: dict[str, Any]) -> dict[str, Any]:
    body = json.loads(job["request_json"])
    destination = _destination(body["destination_id"])
    key = destination["connector_key"]
    _live_destination_guard(destination)
    started = time.monotonic()
    steps: list[dict[str, Any]] = []
    if key == "assisted":
        pack = dispatch(
            key,
            "build_assist_pack",
            {"rendition": body["test_rendition"], "destination": destination},
        )
        caption = storage.put(
            f"certifications/{job['id']}/caption.txt",
            pack["caption"].encode(),
            "text/plain; charset=utf-8",
        )
        steps.append(
            {
                "step": "build_assist_pack",
                "ok": bool(pack["checklist"]),
                "files": [caption],
                "deep_link": pack["deep_link"],
                "checklist": pack["checklist"],
            }
        )
    else:
        rendition = {
            **body["test_rendition"],
            "title": f"IncuBrix certification test {job['id'][:8]}",
            "body": f"Temporary certification test. This post is removed immediately after verification. Ref {job['id'][:8]}.",
            "is_promotional": False,
        }
        try:
            post_started = time.monotonic()
            pub = dispatch(
                key,
                "publish",
                {
                    "rendition": rendition,
                    "destination": destination,
                    "idempotency_key": job["id"],
                },
                simulation=settings.simulation_mode,
            )
            post_live_monotonic = time.monotonic()
            steps.append({"step": "publish", "ok": pub.get("status") == "live", **pub})
        except DistributionError as exc:
            pub = None
            post_live_monotonic = None
            steps.append(
                {
                    "step": "publish",
                    "ok": False,
                    "error": {
                        "code": exc.code,
                        "message": exc.message,
                        "details": exc.details or {},
                        "outcome_unknown": exc.code == "PLATFORM_UNAVAILABLE",
                    },
                    "elapsed_seconds": round(time.monotonic() - post_started, 3),
                }
            )
        if pub:
            post_id = str(pub["platform_post_id"])
            try:
                found = dispatch(
                    key,
                    "get_status",
                    {"platform_post_id": post_id, "destination": destination},
                    simulation=settings.simulation_mode,
                )
                steps.append(
                    {"step": "get_status", "ok": found.get("status") == "live", **found}
                )
            except DistributionError as exc:
                steps.append(
                    {
                        "step": "get_status",
                        "ok": False,
                        "error": {"code": exc.code, "message": exc.message},
                    }
                )
            delete_started = time.monotonic()
            try:
                deleted = dispatch(
                    key,
                    "delete",
                    {"platform_post_id": post_id, "destination": destination},
                    simulation=settings.simulation_mode,
                )
                steps.append(
                    {
                        "step": "delete",
                        "ok": deleted.get("deleted") is True,
                        **deleted,
                        "elapsed_seconds": round(time.monotonic() - delete_started, 3),
                        "cleanup_elapsed_seconds": round(
                            time.monotonic() - post_live_monotonic, 3
                        ),
                    }
                )
            except DistributionError as exc:
                steps.append(
                    {
                        "step": "delete",
                        "ok": False,
                        "elapsed_seconds": round(time.monotonic() - delete_started, 3),
                        "cleanup_elapsed_seconds": round(
                            time.monotonic() - post_live_monotonic, 3
                        ),
                        "error": {"code": exc.code, "message": exc.message},
                    }
                )
    passed = all(step["ok"] for step in steps)
    status = (
        ("simulated_pass" if settings.simulation_mode else "passed")
        if passed
        else "failed"
    )
    duration_s = time.monotonic() - started
    evidence = {
        "certification_id": job["id"],
        "destination_id": destination["id"],
        "connector_key": key,
        "status": status,
        "mode": "simulation" if settings.simulation_mode else "authorized_sandbox",
        "steps": steps,
        "test_rendition": body["test_rendition"],
        "started_at": job["created_at"],
        "finished_at": utcnow(),
        "cleanup_deadline_minutes": 10,
        "cleanup_deadline_met": key == "assisted"
        or any(
            step.get("step") == "delete"
            and step.get("ok")
            and step.get("cleanup_elapsed_seconds", 601) <= 600
            for step in steps
        ),
        "cleanup_required_manual": any(
            step.get("error", {}).get("outcome_unknown") for step in steps
        ),
        "duration_seconds": round(duration_s, 3),
    }
    evidence_url = storage.put(
        f"certifications/{job['id']}/evidence.json",
        json.dumps(evidence, ensure_ascii=False, indent=2).encode(),
        "application/json",
    )
    with connect() as db:
        db.execute(
            "INSERT INTO certifications (id,destination_id,connector_key,status,evidence_json,created_at,finished_at) VALUES (?,?,?,?,?,?,?)",
            (
                job["id"],
                destination["id"],
                key,
                status,
                json.dumps(evidence),
                job["created_at"],
                utcnow(),
            ),
        )
        if status == "passed":
            db.execute(
                "UPDATE destinations SET last_certified_at=?,level=? WHERE id=?",
                (utcnow(), "L1" if key == "assisted" else "L3", destination["id"]),
            )
    warnings = (
        [
            "Simulated connector exercise only; this is not a real platform certification."
        ]
        if settings.simulation_mode
        else []
    )
    result = {
        **evidence,
        "evidence_url": evidence_url,
        "duration_ms": round((time.monotonic() - started) * 1000, 2),
    }
    if not passed:
        raise DistributionError(
            "CERTIFICATION_FAILED",
            "One or more certification operations failed.",
            details={
                "steps": steps,
                "evidence_url": evidence_url,
                "cleanup_required_manual": evidence["cleanup_required_manual"],
            },
            status_code=422,
        )
    return _save_result(job, result, _manifest(job, started, warnings))


def run_job(job_id: str) -> None:
    with connect() as db:
        row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row or row["status"] not in {"queued", "running"}:
            return
        job = dict(row)
        db.execute(
            "UPDATE jobs SET status='running',progress=5,updated_at=? WHERE id=?",
            (utcnow(), job_id),
        )
    started = time.monotonic()
    try:
        if job["kind"] == "publish":
            result = _run_publication(job)
        elif job["kind"] == "certification":
            result = _run_certification(job)
        elif job["kind"] == "catalogue_import":
            body = json.loads(job["request_json"])
            result = import_csv(body["csv_utf8"].encode("utf-8"))
            result = _save_result(job, result, _manifest(job, started))
        else:
            raise DistributionError(
                "INVALID_REQUEST",
                f"Unsupported job kind '{job['kind']}'.",
                status_code=400,
            )
        with connect() as db:
            db.execute(
                "UPDATE jobs SET status='succeeded',progress=100,result_json=?,updated_at=? WHERE id=?",
                (json.dumps(result, ensure_ascii=False), utcnow(), job_id),
            )
        _audit(
            job_id, job.get("correlation_id"), "job.succeeded", {"kind": job["kind"]}
        )
        _send_callback(
            job,
            {
                "job_id": job_id,
                "status": "succeeded",
                "result_url": result.get("result_url"),
            },
        )
    except DistributionError as exc:
        error = {
            "code": exc.code,
            "message": exc.message,
            "retryable": exc.retryable,
            "details": exc.details or {},
        }
        failed_result = None
        try:
            failed_result = _save_result(
                job,
                {"error": error},
                _manifest(
                    job, started, ["operation failed; inspect the error and evidence"]
                ),
            )
        except DistributionError:
            pass
        with connect() as db:
            db.execute(
                "UPDATE jobs SET status='failed',progress=100,error_json=?,result_json=?,updated_at=? WHERE id=?",
                (
                    json.dumps(error),
                    json.dumps(failed_result) if failed_result else None,
                    utcnow(),
                    job_id,
                ),
            )
            failed_request = json.loads(job["request_json"])
            if job["kind"] == "certification":
                destination_id = failed_request.get("destination_id")
                destination = db.execute(
                    "SELECT level FROM destinations WHERE id=?", (destination_id,)
                ).fetchone()
                if destination and (
                    destination["level"] != "L0"
                    or failed_request.get("recertification")
                ):
                    db.execute(
                        "UPDATE destinations SET enabled=0,level='L0' WHERE id=?",
                        (destination_id,),
                    )
                    event = (
                        "destination.recertification_failed"
                        if failed_request.get("recertification")
                        else "destination.certification_failed"
                    )
                    db.execute(
                        "INSERT INTO audit_events (id,job_id,correlation_id,event_type,details_json,created_at) VALUES (?,?,?,?,?,?)",
                        (
                            str(uuid.uuid4()),
                            job_id,
                            job.get("correlation_id"),
                            event,
                            json.dumps(
                                {
                                    "destination_id": destination_id,
                                    "alert": "destination disabled pending owner review",
                                }
                            ),
                            utcnow(),
                        ),
                    )
        _audit(job_id, job.get("correlation_id"), "job.failed", {"code": exc.code})
        _send_callback(job, {"job_id": job_id, "status": "failed", "error": error})
    except Exception as exc:  # noqa: BLE001 - worker boundary persists failures without logging request data
        error = {
            "code": "PLATFORM_UNAVAILABLE",
            "message": "Job failed unexpectedly.",
            "retryable": True,
            "details": {"type": type(exc).__name__},
        }
        failed_result = None
        try:
            failed_result = _save_result(
                job,
                {"error": error},
                _manifest(job, started, ["operation failed unexpectedly"]),
            )
        except DistributionError:
            pass
        with connect() as db:
            db.execute(
                "UPDATE jobs SET status='failed',progress=100,error_json=?,result_json=?,updated_at=? WHERE id=?",
                (
                    json.dumps(error),
                    json.dumps(failed_result) if failed_result else None,
                    utcnow(),
                    job_id,
                ),
            )
        _audit(
            job_id,
            job.get("correlation_id"),
            "job.failed",
            {"code": error["code"], "exception_type": type(exc).__name__},
        )
        _send_callback(job, {"job_id": job_id, "status": "failed", "error": error})


def _send_callback(job: dict[str, Any], payload: dict[str, Any]) -> None:
    callback = job.get("callback_url")
    if not callback or not settings.callback_hmac_secret:
        return
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    signature = hmac.new(
        settings.callback_hmac_secret.encode(), raw, hashlib.sha256
    ).hexdigest()
    try:
        httpx.post(
            callback,
            content=raw,
            headers={
                "Content-Type": "application/json",
                "X-IncuBrix-Signature": f"sha256={signature}",
            },
            timeout=5,
            follow_redirects=False,
        )
    except httpx.HTTPError:
        # Callback delivery is best effort; the canonical job state remains available through GET /jobs.
        return


def recover_queued_jobs() -> list[str]:
    with connect() as db:
        # A crash during a live publish has an unknowable outcome; fail closed to avoid duplicate posts.
        db.execute(
            "UPDATE jobs SET status='failed',progress=100,error_json=?,updated_at=? WHERE status='running'",
            (
                json.dumps(
                    {
                        "code": "PLATFORM_UNAVAILABLE",
                        "message": "Worker restarted during an operation; outcome requires reconciliation and was not retried.",
                        "retryable": False,
                        "details": {},
                    }
                ),
                utcnow(),
            ),
        )
        return [
            row[0]
            for row in db.execute(
                "SELECT id FROM jobs WHERE status='queued' ORDER BY created_at"
            )
        ]


def enqueue_due_recertifications() -> int:
    if settings.simulation_mode:
        return 0
    cutoff = (datetime.now(UTC) - timedelta(days=29)).isoformat()
    with connect() as db:
        due = [
            dict(row)
            for row in db.execute(
                "SELECT id,last_certified_at,connector_key FROM destinations WHERE enabled=1 AND level IN ('L1','L2','L3','L4') AND last_certified_at IS NOT NULL AND last_certified_at<=?",
                (cutoff,),
            )
        ]
    queued = 0
    for destination in due:
        request = {
            "destination_id": destination["id"],
            "test_rendition": {
                "title": "Scheduled recertification",
                "body": "Temporary local test.",
            },
            "recertification": True,
        }
        key = f"scheduled-recert-{destination['id']}-{destination['last_certified_at']}"
        job, created = create_job("certification", request, key)
        queued += int(created)
        if created:
            with connect() as db:
                db.execute(
                    "INSERT INTO audit_events (id,job_id,correlation_id,event_type,details_json,created_at) VALUES (?,?,?,?,?,?)",
                    (
                        str(uuid.uuid4()),
                        job["id"],
                        None,
                        "destination.recertification_queued",
                        json.dumps({"destination_id": destination["id"]}),
                        utcnow(),
                    ),
                )
    return queued

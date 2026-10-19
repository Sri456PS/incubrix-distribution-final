from __future__ import annotations

import json

import httpx
import pytest

from app import connectors, jobs
from app.errors import ERROR_CODES, DistributionError


def test_rate_limit_wait_uses_retry_after_and_retries_once(monkeypatch):
    replies = [
        httpx.Response(
            429,
            headers={"Retry-After": "0.25"},
            request=httpx.Request("POST", "https://instance.test"),
        ),
        httpx.Response(
            200,
            json={"ok": True},
            request=httpx.Request("POST", "https://instance.test"),
        ),
    ]
    calls, waits = [], []
    monkeypatch.setattr(
        httpx,
        "request",
        lambda *args, **kwargs: calls.append((args, kwargs)) or replies.pop(0),
    )
    monkeypatch.setattr(connectors.time, "sleep", waits.append)
    monkeypatch.setattr(connectors.settings, "max_rate_limit_wait_seconds", 1)
    response = connectors._request("POST", "https://instance.test")
    assert response.status_code == 200
    assert len(calls) == 2
    assert waits == [0.25]


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (401, "CONNECTION_AUTH_EXPIRED"),
        (422, "PLATFORM_CONTENT_REJECTED"),
        (503, "PLATFORM_UNAVAILABLE"),
    ],
)
def test_platform_errors_map_to_shared_codes(status, code):
    response = httpx.Response(
        status,
        json={"message": "test failure"},
        headers={"Retry-After": "9"},
        request=httpx.Request("GET", "https://instance.test"),
    )
    with pytest.raises(DistributionError) as raised:
        connectors._auth_error(response)
    assert raised.value.code == code
    assert raised.value.retryable is ERROR_CODES[code]
    if status == 503:
        assert raised.value.retryable is True


def test_connector_publish_timeout_is_cached_without_a_duplicate_call(
    client, add_destination, monkeypatch
):
    from app import main

    destination = add_destination("discourse")
    calls = []

    def timeout(*args, **kwargs):
        calls.append(1)
        raise DistributionError(
            "PLATFORM_UNAVAILABLE",
            "Timed out after request dispatch.",
            True,
            {"outcome_unknown": True},
            504,
        )

    monkeypatch.setattr(main, "dispatch", timeout)
    payload = {
        "rendition": {"title": "Test", "body": "Body"},
        "destination": destination,
        "connection": {"api_key": "sensitive-test-token"},
    }
    headers = {"Idempotency-Key": "uncertain-publish"}
    first = client.post(
        "/connector/v1/discourse/publish", json=payload, headers=headers
    )
    second = client.post(
        "/connector/v1/discourse/publish", json=payload, headers=headers
    )
    assert first.status_code == second.status_code == 504
    assert first.json() == second.json()
    assert calls == [1]
    from app.database import connect

    with connect() as db:
        saved = json.dumps(
            [dict(row) for row in db.execute("SELECT * FROM idempotency")]
        )
    assert "sensitive-test-token" not in saved


def test_certification_attempts_cleanup_and_persists_failed_evidence(
    client, add_destination, monkeypatch
):
    destination = add_destination("lemmy")
    calls = []

    def dispatch(key, method, payload, **kwargs):
        calls.append(method)
        if method == "publish":
            return {
                "platform_post_id": "mock-post-1",
                "live_url": "http://testserver/post/mock-post-1",
                "status": "live",
            }
        if method == "get_status":
            raise DistributionError(
                "PLATFORM_UNAVAILABLE",
                "Status endpoint returned an invalid response.",
                True,
            )
        if method == "delete":
            return {"deleted": True}
        raise AssertionError(method)

    monkeypatch.setattr(jobs, "dispatch", dispatch)
    job_id = "failed-certification-test"
    job = {
        "id": job_id,
        "destination_id": destination["id"],
        "request_json": json.dumps(
            {
                "destination_id": destination["id"],
                "test_rendition": {"title": "Test", "body": "Body"},
            }
        ),
        "created_at": jobs.utcnow(),
    }
    with pytest.raises(DistributionError) as raised:
        jobs._run_certification(job)
    assert raised.value.code == "CERTIFICATION_FAILED"
    assert calls == ["publish", "get_status", "delete"]
    saved, _ = jobs.storage.get(f"certifications/{job_id}/evidence.json")
    evidence = json.loads(saved)
    assert evidence["status"] == "failed"
    assert evidence["steps"][-1]["step"] == "delete"
    assert evidence["steps"][-1]["ok"] is True

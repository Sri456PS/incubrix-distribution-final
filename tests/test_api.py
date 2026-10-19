from __future__ import annotations

import time

from conftest import destination_payload
from openapi_spec_validator import validate_spec


def wait_job(client, job_id):
    for _ in range(100):
        response = client.get(f"/api/v1/jobs/{job_id}")
        assert response.status_code == 200
        job = response.json()
        if job["status"] in {"succeeded", "failed"}:
            return job
        time.sleep(0.03)
    raise AssertionError("job did not finish within 3 seconds")


def test_health_readiness_and_structured_correlation(client):
    assert client.get("/health").json() == {"status": "ok"}
    ready = client.get("/ready")
    assert ready.status_code == 200
    assert ready.json()["checks"] == {"database": True, "storage": True}
    response = client.get(
        "/api/v1/connectors", headers={"X-Correlation-Id": "corr-test"}
    )
    assert response.headers["X-Correlation-Id"] == "corr-test"


def test_destination_requires_idempotency_and_replays(client):
    payload = destination_payload()
    assert client.post("/api/v1/destinations", json=payload).status_code == 400
    headers = {"Idempotency-Key": "dest-once"}
    first = client.post("/api/v1/destinations", json=payload, headers=headers)
    second = client.post("/api/v1/destinations", json=payload, headers=headers)
    assert first.status_code == second.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    changed = {**payload, "name": "Different"}
    conflict = client.post("/api/v1/destinations", json=changed, headers=headers)
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"


def test_secret_material_is_rejected_from_destination_settings(client):
    payload = destination_payload(settings={"api_key": "not-a-secret"})
    response = client.post(
        "/api/v1/destinations",
        json=payload,
        headers={"Idempotency-Key": "secret-rejection"},
    )
    assert response.status_code == 422


def test_direct_live_connector_publish_requires_authorized_sandbox(
    client, add_destination, monkeypatch
):
    from app import main
    from app.config import settings

    destination = add_destination()
    monkeypatch.setattr(settings, "simulation_mode", False)
    calls = []
    monkeypatch.setattr(
        main, "dispatch", lambda *args, **kwargs: calls.append(args) or {"ok": True}
    )
    response = client.post(
        "/connector/v1/discourse/publish",
        json={
            "rendition": {"title": "Test", "body": "Body"},
            "destination": destination,
        },
        headers={"Idempotency-Key": "unsafe-direct-publish"},
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "CONNECTOR_DISABLED"
    assert calls == []


def test_connector_contract_validate_publish_status_delete_and_idempotency(
    client, add_destination
):
    destination = add_destination("discourse")
    headers = {"Idempotency-Key": "contract-publish-1"}
    body = {
        "rendition": {"title": "A test", "body": "Hello"},
        "destination": destination,
    }
    invalid = client.post(
        "/connector/v1/discourse/validate",
        json={"rendition": {"title": "x" * 260}, "destination": destination},
        headers={"Idempotency-Key": "validate-1"},
    )
    assert invalid.status_code == 200
    assert invalid.json()["ok"] is False
    first = client.post("/connector/v1/discourse/publish", json=body, headers=headers)
    second = client.post("/connector/v1/discourse/publish", json=body, headers=headers)
    assert first.status_code == second.status_code == 200
    assert set(first.json()) == {"platform_post_id", "live_url", "status"}
    assert first.json()["platform_post_id"] == second.json()["platform_post_id"]
    pid = first.json()["platform_post_id"]
    status = client.post(
        "/connector/v1/discourse/get_status",
        json={"platform_post_id": pid},
        headers={"Idempotency-Key": "status-1"},
    )
    assert status.json() == {"status": "live", "live_url": first.json()["live_url"]}
    deleted = client.post(
        "/connector/v1/discourse/delete",
        json={"platform_post_id": pid},
        headers={"Idempotency-Key": "delete-1"},
    )
    assert deleted.json() == {"deleted": True}


def test_direct_connector_publish_enforces_inline_destination_rules(client):
    response = client.post(
        "/connector/v1/discourse/publish",
        json={
            "rendition": {
                "title": "Promotion",
                "body": "Buy this product.",
                "is_promotional": True,
            },
            "destination": {
                "id": "unregistered-destination",
                "url": "https://forum.example",
                "self_promo_allowed": False,
            },
            "connection": {},
        },
        headers={"Idempotency-Key": "inline-promotion-block"},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "PLATFORM_CONTENT_REJECTED"


def test_publish_background_job_idempotency_and_manifest(client, add_destination):
    destination = add_destination("lemmy", settings={"community_id": 10})
    payload = {
        "destination_id": destination["id"],
        "rendition": {"title": "Video strategy", "body": "An article"},
    }
    headers = {"Idempotency-Key": "async-publication"}
    first = client.post("/api/v1/publications", json=payload, headers=headers)
    second = client.post("/api/v1/publications", json=payload, headers=headers)
    assert first.status_code == second.status_code == 202
    assert first.json()["job_id"] == second.json()["job_id"]
    job = wait_job(client, first.json()["job_id"])
    assert job["status"] == "succeeded", job
    result = client.get(f"/api/v1/jobs/{job['job_id']}/result").json()
    assert result["status"] == "live"
    assert result["manifest"]["provider"] == "lemmy"
    assert result["manifest"]["cost_estimate"]["amount"] == 0
    assert result["result_url"].endswith("/result.json")
    simulated = client.get(result["live_url"])
    assert simulated.status_code == 200


def test_rule_policy_blocks_second_post_and_promotion(client, add_destination):
    destination = add_destination(
        "discourse",
        frequency_cap=1,
        frequency_window_seconds=3600,
        self_promo_allowed=False,
    )
    payload = {
        "destination_id": destination["id"],
        "rendition": {"title": "First", "body": "Post"},
    }
    first = client.post(
        "/api/v1/publications", json=payload, headers={"Idempotency-Key": "first-post"}
    )
    assert wait_job(client, first.json()["job_id"])["status"] == "succeeded"
    promotional = {
        **payload,
        "rendition": {"title": "Promo", "body": "Buy", "is_promotional": True},
    }
    rejected = client.post(
        "/api/v1/publications",
        json=promotional,
        headers={"Idempotency-Key": "promo-post"},
    )
    assert wait_job(client, rejected.json()["job_id"])["status"] == "failed"
    assert (
        wait_job(client, rejected.json()["job_id"])["error"]["code"]
        == "PLATFORM_CONTENT_REJECTED"
    )
    second = client.post(
        "/api/v1/publications", json=payload, headers={"Idempotency-Key": "second-post"}
    )
    assert (
        wait_job(client, second.json()["job_id"])["error"]["code"]
        == "DEST_RULE_FREQUENCY_CAP"
    )


def test_publication_adds_missing_required_ai_disclosure(client, add_destination):
    destination = add_destination(disclosure_required=True)
    response = client.post(
        "/api/v1/publications",
        json={
            "destination_id": destination["id"],
            "rendition": {
                "title": "AI draft",
                "body": "Draft text.",
                "ai_generated": True,
            },
        },
        headers={"Idempotency-Key": "missing-ai-disclosure"},
    )
    job = wait_job(client, response.json()["job_id"])
    assert job["status"] == "succeeded"
    result = client.get(f"/api/v1/jobs/{job['job_id']}/result").json()
    simulated_post = client.get(result["live_url"]).json()
    assert "AI-generated content." in simulated_post["body"]
    assert result["manifest"]["warnings"] == [
        "simulation mode: no community received a real post"
    ]


def test_certification_runs_all_six_simulated_connectors_and_stores_evidence(
    client, add_destination
):
    for connector in (
        "discourse",
        "lemmy",
        "devto",
        "ghost",
        "signed_webhook",
        "assisted",
    ):
        destination = add_destination(connector)
        request = {
            "destination_id": destination["id"],
            "test_rendition": {"title": "Test", "body": "Temporary local test."},
        }
        response = client.post(
            "/api/v1/certifications",
            json=request,
            headers={"Idempotency-Key": f"cert-{connector}"},
        )
        assert response.status_code == 202
        job = wait_job(client, response.json()["job_id"])
        assert job["status"] == "succeeded", (connector, job)
        result = client.get(f"/api/v1/jobs/{job['job_id']}/result").json()
        assert result["mode"] == "simulation"
        assert result["evidence_url"].endswith("evidence.json")
        if connector == "assisted":
            assert result["steps"][0]["step"] == "build_assist_pack"
        else:
            assert [step["step"] for step in result["steps"]] == [
                "publish",
                "get_status",
                "delete",
            ]
            assert all(step["ok"] for step in result["steps"])


def test_rule_proposal_requires_human_confirmation(client, add_destination):
    dest = add_destination()
    proposal = client.post(
        f"/api/v1/destinations/{dest['id']}/rules/propose",
        json={"rules_text": "No self-promotion. AI-generated posts must be disclosed."},
        headers={"Idempotency-Key": "rules-proposal"},
    )
    assert proposal.status_code == 200
    assert proposal.json()["self_promo_allowed"] is False
    assert proposal.json()["human_confirmation_required"] is True
    unavailable = client.post(
        "/api/v1/publications",
        json={"destination_id": dest["id"], "rendition": {"title": "T", "body": "B"}},
        headers={"Idempotency-Key": "rules-unconfirmed"},
    )
    assert (
        wait_job(client, unavailable.json()["job_id"])["error"]["code"]
        == "CONNECTOR_DISABLED"
    )
    confirmed = client.post(
        f"/api/v1/destinations/{dest['id']}/rules/confirm",
        json={"rules": {"self_promo_allowed": False, "disclosure_required": True}},
        headers={"Idempotency-Key": "rules-confirm"},
    )
    assert confirmed.json()["rules_confirmed"] is True


def test_file_upload_is_idempotent_and_readable(client):
    headers = {"Idempotency-Key": "asset-1"}
    first = client.post(
        "/api/v1/assets",
        files={"file": ("hello.txt", b"hello", "text/plain")},
        headers=headers,
    )
    second = client.post(
        "/api/v1/assets",
        files={"file": ("hello.txt", b"hello", "text/plain")},
        headers=headers,
    )
    assert first.status_code == second.status_code == 201
    assert first.json() == second.json()
    file_response = client.get(first.json()["file_url"])
    assert file_response.status_code == 200
    assert file_response.content == b"hello"


def test_openapi_is_31_and_valid(client):
    spec = client.get("/openapi.json").json()
    assert spec["openapi"].startswith("3.1.")
    assert all(
        path.startswith(("/api/v1/", "/connector/v1/")) or path in {"/health", "/ready"}
        for path in spec["paths"]
    )
    validate_spec(spec)


def test_recommendation_evaluation_uses_labeled_cases(client, add_destination):
    destination = add_destination(
        "discourse", name="AI Creators", audience_tags=["ai", "creators"]
    )
    response = client.post(
        "/api/v1/recommendations/evaluate",
        json={
            "top_k": 1,
            "cases": [
                {
                    "content_tags": ["ai", "creators"],
                    "language": "en",
                    "relevant_destination_ids": [destination["id"]],
                }
            ],
        },
        headers={"Idempotency-Key": "evaluation-case-1"},
    )
    assert response.status_code == 200
    result = response.json()
    assert result["labeled_set_supplied_by_user"] is True
    assert result["precision_at_k"] == 1.0
    assert result["case_results"][0]["predicted_top_k"] == [destination["id"]]

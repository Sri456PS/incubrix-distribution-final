from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_path", str(tmp_path / "app.db"))
    monkeypatch.setattr(settings, "storage_path", str(tmp_path / "objects"))
    monkeypatch.setattr(settings, "storage_backend", "local")
    monkeypatch.setattr(settings, "simulation_mode", True)
    monkeypatch.setattr(settings, "public_base_url", "http://testserver")
    monkeypatch.setattr(settings, "callback_hmac_secret", "test-callback-secret")
    monkeypatch.setattr(settings, "api_rate_limit_per_minute", 10000)
    with TestClient(app) as test_client:
        yield test_client


def destination_payload(connector: str = "discourse", **overrides):
    body = {
        "name": f"Local {connector}",
        "url": f"http://{connector}.test",
        "platform_key": connector,
        "surface": "topic"
        if connector == "discourse"
        else "community"
        if connector == "lemmy"
        else "article"
        if connector == "devto"
        else "post"
        if connector == "ghost"
        else "partner"
        if connector == "signed_webhook"
        else "pack",
        "category": "community",
        "audience_tags": ["ai", "creators"],
        "language": "en",
        "frequency_cap": 0,
        "self_promo_allowed": True,
        "link_policy": "allowed",
        "disclosure_required": False,
        "automation_allowed": connector != "assisted",
        "connector_key": connector,
        "settings": {},
        "rules_confirmed": True,
    }
    body.update(overrides)
    return body


@pytest.fixture
def add_destination(client):
    def add(connector="discourse", **overrides):
        response = client.post(
            "/api/v1/destinations",
            json=destination_payload(connector, **overrides),
            headers={
                "Idempotency-Key": f"dest-{connector}-{overrides.get('name', 'default')}"
            },
        )
        assert response.status_code == 201, response.text
        return response.json()

    return add

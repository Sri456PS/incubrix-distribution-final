from __future__ import annotations

import yaml

from app.connectors import CONNECTOR_KEYS, MANIFESTS
from app.errors import ERROR_CODES


def test_six_required_manifests_match_appendix_connector_keys():
    assert set(CONNECTOR_KEYS) == {
        "discourse",
        "lemmy",
        "devto",
        "ghost",
        "signed_webhook",
        "assisted",
    }
    for key, manifest in MANIFESTS.items():
        assert manifest["key"] == key
        assert set(manifest) == {
            "key",
            "version",
            "tier",
            "auth",
            "surfaces",
            "rate_limits",
            "capabilities",
            "error_map",
            "instance_param",
        }
        assert isinstance(manifest["surfaces"], list) and manifest["surfaces"]
        assert set(manifest["surfaces"][0]) == {"key", "content_types", "spec"}
        assert set(manifest["rate_limits"]["per_connection"]) == {
            "requests",
            "per_seconds",
        }
        assert all(code.isupper() for code in manifest["error_map"].values())


def test_all_manifest_files_are_valid_yaml_and_match_loaded_manifest():
    for key in CONNECTOR_KEYS:
        with open(f"connectors/{key}.yaml", encoding="utf-8") as stream:
            assert yaml.safe_load(stream) == MANIFESTS[key]


def test_shared_error_codes_from_appendix_are_present():
    assert {
        "DEST_SPEC_VIOLATION",
        "DEST_RULE_FREQUENCY_CAP",
        "CONNECTION_AUTH_EXPIRED",
        "PLATFORM_RATE_LIMIT",
        "PLATFORM_QUOTA_EXHAUSTED",
        "PLATFORM_CONTENT_REJECTED",
        "PLATFORM_UNAVAILABLE",
        "CONNECTOR_DISABLED",
    } <= set(ERROR_CODES)

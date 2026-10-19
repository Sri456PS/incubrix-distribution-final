from __future__ import annotations

import csv
import io

from app import catalogue
from app.database import connect


def csv_data(rows):
    output = io.StringIO()
    fields = [
        "name",
        "url",
        "platform_key",
        "surface",
        "category",
        "audience_tags",
        "language",
        "rules_url",
        "connector_key",
        "est_audience",
        "frequency_cap",
        "automation_allowed",
    ]
    writer = csv.DictWriter(output, fieldnames=fields)
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue().encode()


def row(name, url, language="en"):
    return {
        "name": name,
        "url": url,
        "platform_key": "discourse",
        "surface": "topic",
        "category": "community",
        "audience_tags": "ai;creators",
        "language": language,
        "rules_url": url + "/rules",
        "connector_key": "discourse",
        "est_audience": "100",
    }


def test_catalogue_import_normalizes_deduplicates_reports_bad_rows_and_waits_for_rules_confirmation(
    client, monkeypatch
):
    monkeypatch.setattr(catalogue, "reachable", lambda url: True)
    content = csv_data(
        [
            row("One", "https://forum.example/"),
            row("One duplicate", "https://forum.example"),
            row("Bad URL", "javascript:alert(1)"),
            row("Deutsch", "https://de.example/", "de"),
        ]
    )
    report = catalogue.import_csv(content)
    assert report["rows_seen"] == 4
    assert report["imported"] == 2
    assert report["duplicates"] == 1
    assert len(report["errors"]) == 1
    assert report["performance_target_met"] is True
    with connect() as db:
        saved = db.execute(
            "SELECT rules_confirmed,relevance_score,relevance_reason,language FROM destinations ORDER BY language"
        ).fetchall()
    assert all(not item["rules_confirmed"] for item in saved)
    assert all(
        1 <= item["relevance_score"] <= 5 and item["relevance_reason"] for item in saved
    )


def test_thousand_rows_meet_five_minute_import_target(client, monkeypatch):
    monkeypatch.setattr(catalogue, "reachable", lambda url: True)
    rows = [
        row(f"Community {i}", f"https://community-{i}.example") for i in range(1000)
    ]
    report = catalogue.import_csv(csv_data(rows))
    assert report["rows_seen"] == 1000
    assert report["imported"] == 1000
    assert report["performance_target_met"] is True
    assert report["elapsed_seconds"] < 300


def test_import_rejects_unsupported_surface_and_malformed_boolean(client, monkeypatch):
    monkeypatch.setattr(catalogue, "reachable", lambda url: True)
    invalid_surface = row("Bad surface", "https://surface.example")
    invalid_surface["surface"] = "private_message"
    invalid_boolean = row("Bad boolean", "https://boolean.example")
    invalid_boolean["automation_allowed"] = "sometimes"
    report = catalogue.import_csv(csv_data([invalid_surface, invalid_boolean]))
    assert report["imported"] == 0
    assert [entry["row"] for entry in report["errors"]] == [2, 3]
    assert "Unsupported surface" in report["errors"][0]["error"]
    assert "must be a boolean" in report["errors"][1]["error"]

from __future__ import annotations

import csv
import io
import json
import socket
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from ipaddress import ip_address
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from app.config import settings
from app.connectors import CONNECTOR_KEYS, MANIFESTS
from app.database import connect
from app.errors import DistributionError
from app.policy import assess_relevance, decode_destination

REQUIRED_CSV = {
    "name",
    "url",
    "platform_key",
    "surface",
    "category",
    "audience_tags",
    "language",
    "rules_url",
    "connector_key",
}


def normalize_url(value: str) -> str:
    parsed = urlsplit(value.strip())
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise ValueError("URL must be an http(s) URL without embedded credentials.")
    host = parsed.hostname.lower()
    port = parsed.port
    netloc = (
        host
        if port is None
        or (parsed.scheme == "http" and port == 80)
        or (parsed.scheme == "https" and port == 443)
        else f"{host}:{port}"
    )
    path = parsed.path.rstrip("/")
    return urlunsplit((parsed.scheme.lower(), netloc, path, parsed.query, ""))


def _public_http_url(value: str) -> bool:
    parsed = urlsplit(value)
    if not parsed.hostname:
        return False
    try:
        addresses = (
            {ip_address(parsed.hostname)}
            if parsed.hostname.replace(".", "").isdigit()
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
    return bool(addresses) and all(address.is_global for address in addresses)


def reachable(url: str) -> bool:
    if not settings.simulation_mode and not _public_http_url(url):
        return False
    try:
        response = httpx.head(url, follow_redirects=False, timeout=3.0)
        if response.status_code == 405:
            response = httpx.get(
                url, follow_redirects=False, timeout=3.0, headers={"Range": "bytes=0-0"}
            )
        return response.status_code < 500
    except httpx.HTTPError:
        return False


def import_csv(content: bytes) -> dict[str, Any]:
    started = datetime.now(UTC)
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise DistributionError(
            "INVALID_REQUEST", "CSV must be UTF-8 encoded.", status_code=422
        ) from exc
    reader = csv.DictReader(io.StringIO(text))
    headers = {str(h).strip().lower() for h in (reader.fieldnames or [])}
    missing = sorted(REQUIRED_CSV - headers)
    if missing:
        raise DistributionError(
            "INVALID_REQUEST",
            "CSV is missing required columns.",
            details={"missing_columns": missing},
            status_code=422,
        )
    seen: set[tuple[str, str]] = set()
    errors: list[dict[str, Any]] = []
    added: list[str] = []
    duplicates = 0
    processed = 0
    candidates: list[tuple[int, dict[str, str]]] = []
    existing: set[tuple[str, str]] = set()
    with connect() as db:
        existing = {
            (row["platform_key"], row["url"])
            for row in db.execute("SELECT platform_key,url FROM destinations")
        }
    for line, source in enumerate(reader, 2):
        processed += 1
        row: dict[str, str] = {}
        try:
            row = {
                str(k).strip().lower(): (v or "").strip()
                for k, v in source.items()
                if k
            }
            url = normalize_url(row["url"])
            platform = row["platform_key"].lower()
            connector_key = row["connector_key"].lower()
            if connector_key not in CONNECTOR_KEYS:
                raise ValueError(f"Unsupported connector_key: {connector_key}")
            if not row.get("name"):
                raise ValueError("name is required")
            surfaces = {item["key"] for item in MANIFESTS[connector_key]["surfaces"]}
            if row.get("surface") not in surfaces:
                raise ValueError(
                    f"Unsupported surface for {connector_key}: {row.get('surface')}"
                )
            if row.get("rules_url"):
                row["rules_url"] = normalize_url(row["rules_url"])
            for column in ("est_audience", "frequency_cap"):
                if row.get(column) and not row[column].isdigit():
                    raise ValueError(f"{column} must be a non-negative integer")
            for column in (
                "self_promo_allowed",
                "disclosure_required",
                "automation_allowed",
            ):
                if row.get(column) and row[column].lower() not in {
                    "true",
                    "false",
                    "1",
                    "0",
                    "yes",
                    "no",
                }:
                    raise ValueError(f"{column} must be a boolean")
            if row.get("link_policy") and row["link_policy"] not in {
                "allowed",
                "none",
                "no_links",
                "disallowed",
                "canonical_only",
            }:
                raise ValueError("link_policy is unsupported")
            pair = (platform, url)
            if pair in seen or pair in existing:
                duplicates += 1
                continue
            seen.add(pair)
            candidates.append(
                (
                    line,
                    {
                        **row,
                        "url": url,
                        "platform_key": platform,
                        "connector_key": connector_key,
                    },
                )
            )
        except (ValueError, KeyError, TypeError) as exc:
            errors.append({"row": line, "error": str(exc)})
    with ThreadPoolExecutor(max_workers=32) as pool:
        reachable_results = list(
            pool.map(lambda row: reachable(row[1]["url"]), candidates)
        )
    valid: list[tuple[int, dict[str, str]]] = []
    for (line, row), is_reachable in zip(candidates, reachable_results, strict=True):
        if is_reachable:
            valid.append((line, row))
        else:
            errors.append(
                {
                    "row": line,
                    "error": "Destination URL is not reachable or is not a permitted public URL.",
                }
            )
    with connect() as db:
        for line, row in valid:
            try:
                tags = [
                    tag.strip()
                    for tag in row["audience_tags"].split(";")
                    if tag.strip()
                ]
                score, reason = assess_relevance(
                    tags, row["category"], row.get("language") or "en"
                )
                destination_id = str(uuid.uuid4())
                now = datetime.now(UTC).isoformat()
                db.execute(
                    """INSERT INTO destinations (id,name,url,platform_key,surface,category,audience_tags,
                  language,geo,est_audience,rules_url,frequency_cap,self_promo_allowed,link_policy,disclosure_required,
                  automation_allowed,connector_key,relevance_score,relevance_reason,level,last_certified_at,enabled,
                  settings_json,rules_confirmed,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'L0',NULL,1,'{}',0,?)""",
                    (
                        destination_id,
                        row["name"],
                        row["url"],
                        row["platform_key"],
                        row["surface"],
                        row["category"],
                        json.dumps(tags),
                        row.get("language") or "en",
                        row.get("geo") or None,
                        int(row["est_audience"])
                        if row.get("est_audience", "")
                        else None,
                        row["rules_url"] or None,
                        int(row["frequency_cap"])
                        if row.get("frequency_cap", "")
                        else 0,
                        int(
                            row.get("self_promo_allowed", "true").lower()
                            in {"true", "1", "yes"}
                        ),
                        row.get("link_policy", "allowed") or "allowed",
                        int(
                            row.get("disclosure_required", "false").lower()
                            in {"true", "1", "yes"}
                        ),
                        int(
                            row.get("automation_allowed", "true").lower()
                            in {"true", "1", "yes"}
                        ),
                        row["connector_key"].lower(),
                        score,
                        reason,
                        now,
                    ),
                )
                added.append(destination_id)
            except (ValueError, KeyError, TypeError) as exc:
                errors.append({"row": line, "error": str(exc)})
    elapsed = (datetime.now(UTC) - started).total_seconds()
    return {
        "rows_seen": processed,
        "imported": len(added),
        "duplicates": duplicates,
        "errors": errors,
        "destination_ids": added,
        "elapsed_seconds": elapsed,
        "performance_target_met": processed < 1000 or elapsed < 300,
    }


def list_destinations() -> list[dict[str, Any]]:
    with connect() as db:
        return [
            decode_destination(row)
            for row in db.execute("SELECT * FROM destinations ORDER BY created_at DESC")
        ]

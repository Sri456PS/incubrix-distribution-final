from __future__ import annotations

import json
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

from app.errors import DistributionError


def violations(
    rendition: dict[str, Any], destination: dict[str, Any], manifest: dict[str, Any]
) -> list[dict[str, str]]:
    spec = manifest["surfaces"][0]["spec"]
    errors: list[dict[str, str]] = []
    title, body = str(rendition.get("title", "")), str(rendition.get("body", ""))
    if len(title) > spec["title_max_chars"]:
        errors.append(
            {
                "field": "title",
                "code": "DEST_SPEC_VIOLATION",
                "message": f"Title exceeds {spec['title_max_chars']} characters.",
            }
        )
    if len(body) > spec["body_max_chars"]:
        errors.append(
            {
                "field": "body",
                "code": "DEST_SPEC_VIOLATION",
                "message": f"Body exceeds {spec['body_max_chars']} characters.",
            }
        )
    media_type = rendition.get("media_type", "text")
    allowed = manifest["surfaces"][0]["content_types"]
    if media_type not in allowed:
        errors.append(
            {
                "field": "media_type",
                "code": "DEST_SPEC_VIOLATION",
                "message": f"Media type '{media_type}' is not supported.",
            }
        )
    body_links = [
        link.rstrip(".,;:!?)\\]")
        for link in re.findall(r"https?://[^\s<>()]+", body, re.IGNORECASE)
    ]
    canonical_url = str(rendition.get("canonical_url") or "").rstrip(".,;:!?)\\]")
    has_links = bool(body_links or canonical_url)
    if not spec.get("links_allowed", True) and has_links:
        errors.append(
            {
                "field": "body",
                "code": "DEST_SPEC_VIOLATION",
                "message": "Links are not allowed by this destination.",
            }
        )
    if (
        destination.get("link_policy")
        in {
            "none",
            "no_links",
            "disallowed",
        }
        and has_links
    ):
        errors.append(
            {
                "field": "body",
                "code": "DEST_SPEC_VIOLATION",
                "message": "Destination policy disallows links.",
            }
        )
    if destination.get("link_policy") == "canonical_only" and any(
        not canonical_url or link.rstrip(".,;:!?)\\]") != canonical_url
        for link in body_links
    ):
        errors.append(
            {
                "field": "body",
                "code": "DEST_SPEC_VIOLATION",
                "message": "Only the destination's canonical URL is allowed.",
            }
        )
    if rendition.get("is_promotional") and not destination.get(
        "self_promo_allowed", True
    ):
        errors.append(
            {
                "field": "is_promotional",
                "code": "PLATFORM_CONTENT_REJECTED",
                "message": "Self-promotion is prohibited by this destination.",
            }
        )
    disclosure = str(rendition.get("ai_disclosure") or "").strip()
    body_discloses_ai = bool(
        re.search(r"\bAI[- ]generated\b|\bmade with AI\b", body, re.IGNORECASE)
    )
    if (
        destination.get("disclosure_required")
        and rendition.get("ai_generated")
        and not ((disclosure and disclosure in body) or body_discloses_ai)
    ):
        errors.append(
            {
                "field": "ai_disclosure",
                "code": "DEST_SPEC_VIOLATION",
                "message": "Add the required AI-content disclosure before posting.",
            }
        )
    return errors


def enforce_content_rules(
    rendition: dict[str, Any], destination: dict[str, Any], manifest: dict[str, Any]
) -> None:
    if not destination.get("enabled", True):
        raise DistributionError(
            "CONNECTOR_DISABLED", "Destination is disabled.", status_code=409
        )
    if not destination.get("automation_allowed", True):
        raise DistributionError(
            "CONNECTOR_DISABLED",
            "This destination requires a person to post; use the assisted connector.",
            status_code=409,
        )
    if not destination.get("rules_confirmed", True):
        raise DistributionError(
            "CONNECTOR_DISABLED",
            "Destination rules are awaiting human confirmation.",
            status_code=409,
        )
    found = violations(rendition, destination, manifest)
    if found:
        first = found[0]
        code = first["code"]
        raise DistributionError(
            code, first["message"], details={"violations": found}, status_code=422
        )


def enforce_rules(
    db: sqlite3.Connection,
    rendition: dict[str, Any],
    destination: dict[str, Any],
    manifest: dict[str, Any],
) -> None:
    enforce_content_rules(rendition, destination, manifest)
    cap = int(destination.get("frequency_cap", 0) or 0)
    if cap:
        seconds = int(destination.get("frequency_window_seconds", 604800))
        since = (datetime.now(UTC) - timedelta(seconds=seconds)).isoformat()
        row = db.execute(
            "SELECT COUNT(*) n FROM posts WHERE destination_id=? AND created_at>=?",
            (destination["id"], since),
        ).fetchone()
        if int(row["n"]) >= cap:
            raise DistributionError(
                "DEST_RULE_FREQUENCY_CAP",
                "Destination frequency cap reached; retry after the cap window.",
                retryable=True,
                details={"frequency_cap": cap, "window_seconds": seconds},
                status_code=429,
            )


def assess_relevance(
    tags: list[str], category: str, language: str = "en"
) -> tuple[int, str]:
    target = {"ai", "video", "marketing", "saas", "creators", "founders"}
    matched = sorted(target.intersection({str(t).strip().lower() for t in tags}))
    score = min(5, max(1, 1 + len(matched)))
    if language.lower() != "en":
        score = max(1, score - 1)
    reason = f"Matched {len(matched)} IncuBrix audience tags: {', '.join(matched) if matched else 'none'}; category={category}; language={language}."
    return score, reason


def tailor(
    rendition: dict[str, Any], destination: dict[str, Any], manifest: dict[str, Any]
) -> dict[str, Any]:
    result = dict(rendition)
    spec = manifest["surfaces"][0]["spec"]
    warnings: list[str] = []
    limit = int(spec["title_max_chars"])
    title = str(result.get("title", ""))
    if len(title) > limit:
        title = title[:limit].rsplit(" ", 1)[0] or title[:limit]
        warnings.append("Title shortened at a word boundary to fit destination limits.")
    result["title"] = title
    body = str(result.get("body", ""))
    disclosure_suffix = ""
    if destination.get("disclosure_required") and result.get("ai_generated"):
        disclosure = str(result.get("ai_disclosure") or "AI-generated content.")
        result["ai_disclosure"] = disclosure
        body = body.replace(disclosure, "").rstrip()
        disclosure_suffix = f"\n\n{disclosure}"
    body_limit = int(spec["body_max_chars"])
    available = body_limit - len(disclosure_suffix)
    if available < 0:
        body = disclosure_suffix.strip()
        warnings.append(
            "Required AI disclosure exceeds the destination body limit; publication must be reviewed."
        )
    else:
        if len(body) > available:
            body = body[:available].rsplit(" ", 1)[0] or body[:available]
            warnings.append(
                "Body shortened at a word boundary to fit destination limits."
            )
        body = body.rstrip() + disclosure_suffix
    if len(body) > body_limit and not disclosure_suffix:
        body = body[:body_limit].rsplit(" ", 1)[0] or body[:body_limit]
        warnings.append("Body shortened at a word boundary to fit destination limits.")
    result["body"] = body
    result["tailoring_warnings"] = warnings
    result["tailoring_note"] = (
        f"Prepared for {destination.get('surface', 'destination')} ({destination.get('language', 'en')})."
    )
    return result


def decode_destination(row: sqlite3.Row) -> dict[str, Any]:
    out = dict(row)
    for key in ("audience_tags", "settings_json"):
        if key in out:
            try:
                out[key.removesuffix("_json")] = json.loads(out[key])
            except (TypeError, json.JSONDecodeError):
                out[key.removesuffix("_json")] = [] if key == "audience_tags" else {}
    for key in (
        "self_promo_allowed",
        "disclosure_required",
        "automation_allowed",
        "enabled",
        "rules_confirmed",
    ):
        if key in out:
            out[key] = bool(out[key])
    return out

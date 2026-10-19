from __future__ import annotations

from app.connectors import MANIFESTS
from app.policy import assess_relevance, tailor, violations


def test_format_and_media_constraints_are_field_level():
    result = violations(
        {"title": "X" * 300, "body": "ok", "media_type": "audio"},
        {"link_policy": "allowed", "self_promo_allowed": True},
        MANIFESTS["discourse"],
    )
    assert [item["field"] for item in result] == ["title", "media_type"]
    assert all(item["code"] == "DEST_SPEC_VIOLATION" for item in result)


def test_promotion_links_and_disclosure_are_checked():
    result = violations(
        {
            "title": "Title",
            "body": "visit https://example.test",
            "is_promotional": True,
            "ai_generated": True,
        },
        {
            "self_promo_allowed": False,
            "link_policy": "disallowed",
            "disclosure_required": True,
        },
        MANIFESTS["discourse"],
    )
    codes = {item["code"] for item in result}
    assert "PLATFORM_CONTENT_REJECTED" in codes
    assert sum(item["field"] == "ai_disclosure" for item in result) == 1
    assert len(result) == 3


def test_canonical_only_links_allow_only_the_declared_canonical_url():
    destination = {"link_policy": "canonical_only"}
    manifest = MANIFESTS["discourse"]
    rejected = violations(
        {
            "title": "Title",
            "body": "Read https://other.example/page",
            "canonical_url": "https://canonical.example/story",
        },
        destination,
        manifest,
    )
    assert any("canonical URL" in item["message"] for item in rejected)
    accepted = violations(
        {
            "title": "Title",
            "body": "Read https://canonical.example/story.",
            "canonical_url": "https://canonical.example/story",
        },
        destination,
        manifest,
    )
    assert accepted == []


def test_disallowed_link_policy_rejects_canonical_url_field():
    result = violations(
        {"title": "Title", "body": "", "canonical_url": "https://example.test"},
        {"link_policy": "disallowed"},
        MANIFESTS["discourse"],
    )
    assert any("disallows links" in item["message"] for item in result)


def test_relevance_is_bounded_and_explained():
    score, reason = assess_relevance(
        ["AI", "founders", "creators", "video", "saas", "marketing"], "blog"
    )
    assert score == 5
    assert "Matched 6" in reason


def test_tailoring_stops_title_at_word_boundary_and_adds_disclosure():
    source = {
        "title": "A title that is much too long for this short rule",
        "body": "Text",
        "ai_generated": True,
    }
    dest = {"surface": "topic", "language": "en", "disclosure_required": True}
    result = tailor(
        source,
        dest,
        {"surfaces": [{"spec": {"title_max_chars": 20, "body_max_chars": 100}}]},
    )
    assert len(result["title"]) <= 20
    assert "AI-generated content." in result["body"]

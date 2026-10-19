from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import uuid
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import httpx
import yaml

from app.config import settings
from app.database import connect
from app.errors import DistributionError
from app.policy import (
    decode_destination,
    enforce_content_rules,
    enforce_rules,
    violations,
)

CONNECTOR_KEYS = ("discourse", "lemmy", "devto", "ghost", "signed_webhook", "assisted")
MANIFESTS: dict[str, dict[str, Any]] = {}
for _key in CONNECTOR_KEYS:
    with (Path(__file__).resolve().parent.parent / "connectors" / f"{_key}.yaml").open(
        encoding="utf-8"
    ) as _manifest_file:
        MANIFESTS[_key] = yaml.safe_load(_manifest_file)


def manifest(key: str) -> dict[str, Any]:
    try:
        return MANIFESTS[key]
    except KeyError as exc:
        raise DistributionError(
            "CONNECTOR_NOT_FOUND", f"Unknown connector '{key}'.", status_code=404
        ) from exc


def _auth_error(response: httpx.Response, reset: str | None = None) -> None:
    code, retryable, status = "PLATFORM_UNAVAILABLE", True, 502
    if response.status_code == 401 or response.status_code == 403:
        code, retryable, status = "CONNECTION_AUTH_EXPIRED", False, 401
    elif response.status_code == 429:
        code, retryable, status = "PLATFORM_RATE_LIMIT", True, 429
    elif response.status_code in (400, 422):
        code, retryable, status = "PLATFORM_CONTENT_REJECTED", False, 422
    elif response.status_code >= 500:
        code, retryable, status = "PLATFORM_UNAVAILABLE", True, 502
    details: dict[str, Any] = {"platform_status": response.status_code}
    reset_value = (
        reset
        or response.headers.get("Retry-After")
        or response.headers.get("RateLimit-Reset")
        or response.headers.get("X-RateLimit-Reset")
    )
    if reset_value:
        details["retry_after"] = reset_value
    try:
        payload = response.json()
        details["platform_message"] = str(
            payload.get("message", payload.get("error", ""))
        )[:500]
    except (ValueError, AttributeError):
        details["platform_message"] = response.text[:500]
    raise DistributionError(
        code, "Platform request failed.", retryable, details, status
    )


def _request(method: str, url: str, **kwargs: Any) -> httpx.Response:
    for attempt in range(2):
        try:
            response = httpx.request(
                method, url, timeout=settings.connector_timeout_seconds, **kwargs
            )
        except httpx.TimeoutException as exc:
            raise DistributionError(
                "PLATFORM_UNAVAILABLE",
                "Platform request timed out; retry with the same idempotency key.",
                True,
                {"timeout_seconds": settings.connector_timeout_seconds},
                504,
            ) from exc
        except httpx.HTTPError as exc:
            raise DistributionError(
                "PLATFORM_UNAVAILABLE",
                "Platform could not be reached.",
                True,
                {"reason": str(exc)[:300]},
                502,
            ) from exc
        if response.status_code == 429 and attempt == 0:
            raw_reset = (
                response.headers.get("Retry-After")
                or response.headers.get("RateLimit-Reset")
                or response.headers.get("X-RateLimit-Reset")
            )
            delay = None
            if raw_reset:
                try:
                    delay = float(raw_reset)
                    if delay > 10_000_000:
                        delay -= time.time()
                except ValueError:
                    try:
                        delay = (
                            parsedate_to_datetime(raw_reset) - datetime.now(UTC)
                        ).total_seconds()
                    except (TypeError, ValueError, OverflowError):
                        delay = None
            if delay is not None and 0 <= delay <= settings.max_rate_limit_wait_seconds:
                time.sleep(delay)
                continue
        if response.status_code >= 400:
            _auth_error(response)
        return response
    raise DistributionError(
        "PLATFORM_UNAVAILABLE",
        "Request retry loop ended without a response.",
        True,
        status_code=502,
    )


def _connection(key: str, supplied: dict[str, Any]) -> dict[str, Any]:
    env = {
        "discourse": {
            "api_key": settings.discourse_api_key,
            "api_username": settings.discourse_api_username,
            "base_url": settings.discourse_base_url,
        },
        "lemmy": {
            "jwt": settings.lemmy_jwt,
            "community_id": settings.lemmy_community_id,
            "base_url": settings.lemmy_base_url,
        },
        "devto": {
            "api_key": settings.devto_api_key,
            "base_url": settings.devto_base_url,
        },
        "ghost": {
            "admin_api_key": settings.ghost_admin_api_key,
            "base_url": settings.ghost_base_url,
        },
        "signed_webhook": {"secret": settings.webhook_shared_secret},
        "assisted": {},
    }.get(key, {})
    return {**env, **(supplied or {})}


def _base(destination: dict[str, Any], connection: dict[str, Any]) -> str:
    base = str(connection.get("base_url") or destination.get("url") or "").rstrip("/")
    if not base:
        raise DistributionError(
            "INVALID_REQUEST",
            "A destination URL or connection base_url is required.",
            status_code=422,
        )
    return base


def _sim_publish(
    key: str,
    rendition: dict[str, Any],
    destination: dict[str, Any],
    idempotency_key: str,
) -> dict[str, Any]:
    with connect() as db:
        old = db.execute(
            "SELECT * FROM posts WHERE connector_key=? AND idempotency_key=?",
            (key, idempotency_key),
        ).fetchone()
        if old:
            return {
                "platform_post_id": old["platform_post_id"],
                "live_url": old["live_url"],
                "status": old["status"],
            }
        pid = f"sim-{uuid.uuid5(uuid.NAMESPACE_URL, key + ':' + idempotency_key).hex[:18]}"
        live_url = (
            f"{settings.public_base_url.rstrip('/')}/api/v1/simulation/posts/{pid}"
        )
        now = datetime.now(UTC).isoformat()
        db.execute(
            """INSERT INTO posts (id,destination_id,connector_key,platform_post_id,live_url,title,body,
          is_promotional,ai_generated,status,simulation,created_at,idempotency_key)
          VALUES (?,?,?,?,?,?,?,?,?,'live',1,?,?)""",
            (
                pid,
                destination["id"],
                key,
                pid,
                live_url,
                rendition.get("title", ""),
                rendition.get("body", ""),
                int(bool(rendition.get("is_promotional"))),
                int(bool(rendition.get("ai_generated"))),
                now,
                idempotency_key,
            ),
        )
        return {"platform_post_id": pid, "live_url": live_url, "status": "live"}


def _find_post(key: str, platform_post_id: str):
    with connect() as db:
        row = db.execute(
            "SELECT * FROM posts WHERE connector_key=? AND platform_post_id=?",
            (key, platform_post_id),
        ).fetchone()
        return dict(row) if row else None


def _sim_status(key: str, pid: str) -> dict[str, Any]:
    row = _find_post(key, pid)
    if not row:
        raise DistributionError(
            "DESTINATION_NOT_FOUND", "Post was not found.", status_code=404
        )
    return {"status": row["status"], "live_url": row["live_url"]}


def _sim_delete(key: str, pid: str) -> dict[str, bool]:
    with connect() as db:
        row = db.execute(
            "SELECT id FROM posts WHERE connector_key=? AND platform_post_id=?",
            (key, pid),
        ).fetchone()
        if not row:
            raise DistributionError(
                "DESTINATION_NOT_FOUND", "Post was not found.", status_code=404
            )
        db.execute(
            "UPDATE posts SET status='deleted', deleted_at=? WHERE id=?",
            (datetime.now(UTC).isoformat(), row["id"]),
        )
    return {"deleted": True}


def _jwt_for_ghost(api_key: str) -> str:
    try:
        key_id, secret = api_key.split(":", 1)
        issued = int(time.time())
        encode = lambda part: (
            base64.urlsafe_b64encode(json.dumps(part, separators=(",", ":")).encode())
            .rstrip(b"=")
            .decode()
        )
        head = encode({"alg": "HS256", "typ": "JWT", "kid": key_id})
        body = encode({"iat": issued, "exp": issued + 300, "aud": "/admin/"})
        signature = (
            base64.urlsafe_b64encode(
                hmac.new(
                    bytes.fromhex(secret), f"{head}.{body}".encode(), hashlib.sha256
                ).digest()
            )
            .rstrip(b"=")
            .decode()
        )
        token = f"{head}.{body}.{signature}"
    except (ValueError, AttributeError) as exc:
        raise DistributionError(
            "CONNECTION_AUTH_EXPIRED",
            "Ghost Admin API key must use id:hexsecret format.",
            False,
            status_code=401,
        ) from exc
    return token


def _live_publish(
    key: str,
    rendition: dict[str, Any],
    destination: dict[str, Any],
    connection: dict[str, Any],
    idem: str,
) -> dict[str, Any]:
    base = _base(destination, connection)
    title, body = str(rendition.get("title", "")), str(rendition.get("body", ""))
    if key == "discourse":
        headers = {
            "Api-Key": str(connection.get("api_key", "")),
            "Api-Username": str(connection.get("api_username", "system")),
        }
        payload = {"title": title, "raw": body}
        category_id = destination.get("settings", {}).get("category_id")
        if category_id is not None:
            payload["category"] = int(category_id)
        data = _request(
            "POST", f"{base}/posts.json", headers=headers, data=payload
        ).json()
        post = data.get("post", data)
        pid = str(post.get("id"))
        topic_id = post.get("topic_id", pid)
        url = str(post.get("url") or f"{base}/t/{topic_id}")
    elif key == "lemmy":
        community_id = destination.get("settings", {}).get(
            "community_id"
        ) or connection.get("community_id")
        if not community_id:
            raise DistributionError(
                "INVALID_REQUEST",
                "Lemmy destination requires settings.community_id.",
                status_code=422,
            )
        payload = {
            "name": title,
            "body": body,
            "community_id": int(community_id),
            "auth": connection.get("jwt"),
        }
        link = rendition.get("canonical_url")
        if link:
            payload["url"] = link
        data = _request("POST", f"{base}/api/v3/post", json=payload).json()
        post = data.get("post_view", {}).get("post", data.get("post", {}))
        pid = str(post.get("id"))
        url = str(post.get("ap_id") or f"{base}/post/{pid}")
    elif key == "devto":
        headers = {"api-key": str(connection.get("api_key", ""))}
        article = {"title": title, "body_markdown": body, "published": True}
        if rendition.get("canonical_url"):
            article["canonical_url"] = rendition["canonical_url"]
        data = _request(
            "POST", f"{base}/articles", headers=headers, json={"article": article}
        ).json()
        pid, url = (
            str(data["id"]),
            str(data.get("url", f"{base}/articles/{data['id']}")),
        )
    elif key == "ghost":
        api_key = str(connection.get("admin_api_key", ""))
        headers = {"Authorization": f"Ghost {_jwt_for_ghost(api_key)}"}
        html = (
            rendition.get("html")
            or f"<p>{body.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace(chr(10), '<br>')}</p>"
        )
        payload = {
            "posts": [
                {
                    "title": title,
                    "html": html,
                    "status": "published",
                    "tags": [{"name": "IncuBrix"}],
                }
            ]
        }
        data = _request(
            "POST",
            f"{base}/ghost/api/admin/posts/?source=html",
            headers=headers,
            json=payload,
        ).json()
        post = data["posts"][0]
        pid, url = (
            str(post["id"]),
            str(post.get("url", f"{base}/{post.get('slug', '')}/")),
        )
    elif key == "signed_webhook":
        secret = str(connection.get("secret", ""))
        target = str(
            destination.get("settings", {}).get("endpoint")
            or destination.get("url")
            or ""
        )
        if not secret:
            raise DistributionError(
                "CONNECTION_AUTH_EXPIRED",
                "Webhook shared secret is not configured.",
                status_code=401,
            )
        payload = {
            "title": title,
            "body": body,
            "destination_id": destination["id"],
            "idempotency_key": idem,
        }
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        signature = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        data = _request(
            "POST",
            target,
            content=raw,
            headers={
                "Content-Type": "application/json",
                "X-IncuBrix-Signature": f"sha256={signature}",
                "Idempotency-Key": idem,
            },
        ).json()
        pid, url = (
            str(data.get("platform_post_id", data.get("id", idem))),
            str(data.get("live_url", target)),
        )
    else:
        raise DistributionError(
            "METHOD_NOT_SUPPORTED",
            f"Connector '{key}' does not publish.",
            status_code=405,
        )
    with connect() as db:
        old = db.execute(
            "SELECT * FROM posts WHERE connector_key=? AND idempotency_key=?",
            (key, idem),
        ).fetchone()
        if old:
            return {
                "platform_post_id": old["platform_post_id"],
                "live_url": old["live_url"],
                "status": old["status"],
            }
        db.execute(
            """INSERT INTO posts (id,destination_id,connector_key,platform_post_id,live_url,title,body,
          is_promotional,ai_generated,status,simulation,created_at,idempotency_key) VALUES (?,?,?,?,?,?,?,?,?,'live',0,?,?)""",
            (
                str(uuid.uuid4()),
                destination["id"],
                key,
                pid,
                url,
                title,
                body,
                int(bool(rendition.get("is_promotional"))),
                int(bool(rendition.get("ai_generated"))),
                datetime.now(UTC).isoformat(),
                idem,
            ),
        )
    return {"platform_post_id": pid, "live_url": url, "status": "live"}


def _live_status(
    key: str, pid: str, destination: dict[str, Any], connection: dict[str, Any]
) -> dict[str, Any]:
    base = _base(destination, connection)
    if key == "discourse":
        data = _request(
            "GET",
            f"{base}/posts/{pid}.json",
            headers={
                "Api-Key": connection.get("api_key", ""),
                "Api-Username": connection.get("api_username", "system"),
            },
        ).json()
        return {"status": "live", "live_url": str(data.get("url") or f"{base}/p/{pid}")}
    if key == "lemmy":
        data = _request(
            "GET",
            f"{base}/api/v3/post",
            params={"id": pid},
            headers={"Authorization": f"Bearer {connection.get('jwt', '')}"},
        ).json()
        post = data.get("post_view", {}).get("post", data.get("post", {}))
        return {
            "status": "deleted" if post.get("deleted") else "live",
            "live_url": str(post.get("ap_id") or f"{base}/post/{pid}"),
        }
    if key == "devto":
        data = _request(
            "GET",
            f"{base}/articles/{pid}",
            headers={"api-key": connection.get("api_key", "")},
        ).json()
        return {
            "status": "live" if data.get("published", True) else "draft",
            "live_url": str(data.get("url", "")),
        }
    if key == "ghost":
        headers = {
            "Authorization": f"Ghost {_jwt_for_ghost(str(connection.get('admin_api_key', '')))}"
        }
        data = _request(
            "GET",
            f"{base}/ghost/api/admin/posts/{pid}/",
            headers=headers,
            params={"formats": "html"},
        ).json()
        post = data["posts"][0]
        return {
            "status": str(post.get("status", "unknown")),
            "live_url": str(post.get("url", "")),
        }
    if key == "signed_webhook":
        target = str(
            destination.get("settings", {}).get("endpoint")
            or destination.get("url", "")
        )
        data = _request(
            "GET",
            target.rstrip("/") + "/" + pid,
            headers={
                "X-IncuBrix-Signature": hmac.new(
                    str(connection.get("secret", "")).encode(),
                    pid.encode(),
                    hashlib.sha256,
                ).hexdigest()
            },
        ).json()
        return {
            "status": str(data.get("status", "unknown")),
            "live_url": str(data.get("live_url", target)),
        }
    raise DistributionError(
        "METHOD_NOT_SUPPORTED",
        f"Connector '{key}' has no status method.",
        status_code=405,
    )


def _live_delete(
    key: str, pid: str, destination: dict[str, Any], connection: dict[str, Any]
) -> dict[str, bool]:
    base = _base(destination, connection)
    if key == "discourse":
        _request(
            "DELETE",
            f"{base}/posts/{pid}.json",
            headers={
                "Api-Key": connection.get("api_key", ""),
                "Api-Username": connection.get("api_username", "system"),
            },
        )
    elif key == "lemmy":
        _request(
            "POST",
            f"{base}/api/v3/post/delete",
            json={"post_id": int(pid), "deleted": True, "auth": connection.get("jwt")},
        )
    elif key == "devto":
        # Forem's author API has no hard-delete operation; unpublishing removes public visibility.
        _request(
            "PUT",
            f"{base}/articles/{pid}",
            headers={"api-key": connection.get("api_key", "")},
            json={"article": {"published": False}},
        )
    elif key == "ghost":
        headers = {
            "Authorization": f"Ghost {_jwt_for_ghost(str(connection.get('admin_api_key', '')))}"
        }
        current = _request(
            "GET", f"{base}/ghost/api/admin/posts/{pid}/", headers=headers
        ).json()["posts"][0]
        _request(
            "DELETE",
            f"{base}/ghost/api/admin/posts/{pid}/",
            headers=headers,
            params={"updated_at": current["updated_at"]},
        )
    elif key == "signed_webhook":
        target = str(
            destination.get("settings", {}).get("endpoint")
            or destination.get("url", "")
        )
        _request(
            "DELETE", target.rstrip("/") + "/" + pid, headers={"Idempotency-Key": pid}
        )
    else:
        raise DistributionError(
            "METHOD_NOT_SUPPORTED",
            f"Connector '{key}' has no delete method.",
            status_code=405,
        )
    with connect() as db:
        db.execute(
            "UPDATE posts SET status='deleted', deleted_at=? WHERE connector_key=? AND platform_post_id=?",
            (datetime.now(UTC).isoformat(), key, pid),
        )
    return {"deleted": True}


def _live_metrics(
    key: str,
    pid: str,
    destination: dict[str, Any],
    connection: dict[str, Any],
    since: str | None,
) -> dict[str, int]:
    base = _base(destination, connection)
    metrics = {
        "views": 0,
        "likes": 0,
        "comments": 0,
        "shares": 0,
        "saves": 0,
        "watch_time_s": 0,
        "clicks": 0,
    }
    if key == "discourse":
        headers = {
            "Api-Key": str(connection.get("api_key", "")),
            "Api-Username": str(connection.get("api_username", "system")),
        }
        post = _request("GET", f"{base}/posts/{pid}.json", headers=headers).json()
        topic_id = post.get("topic_id")
        if topic_id:
            topic = _request("GET", f"{base}/t/{topic_id}.json", headers=headers).json()
            metrics["views"] = int(topic.get("views", 0) or 0)
            metrics["comments"] = max(0, int(topic.get("reply_count", 0) or 0))
        metrics["likes"] = int(post.get("like_count", 0) or 0)
    elif key == "lemmy":
        headers = {"Authorization": f"Bearer {connection.get('jwt', '')}"}
        data = _request(
            "GET", f"{base}/api/v3/post", params={"id": pid}, headers=headers
        ).json()
        view = data.get("post_view", {})
        counts = view.get("counts", {})
        metrics["likes"] = int(counts.get("upvotes", 0) or 0)
        metrics["comments"] = int(counts.get("comments", 0) or 0)
    elif key == "devto":
        data = _request(
            "GET",
            f"{base}/articles/{pid}",
            headers={"api-key": connection.get("api_key", "")},
        ).json()
        metrics["views"] = int(data.get("page_views_count", 0) or 0)
        metrics["likes"] = int(data.get("public_reactions_count", 0) or 0)
        metrics["comments"] = int(data.get("comments_count", 0) or 0)
    else:
        raise DistributionError(
            "METHOD_NOT_SUPPORTED",
            f"Metrics are not available for '{key}'.",
            status_code=405,
        )
    return metrics


def _assist(rendition: dict[str, Any], destination: dict[str, Any]) -> dict[str, Any]:
    title, body = str(rendition.get("title", "")), str(rendition.get("body", ""))
    url = str(destination.get("url", ""))
    disclosure = (
        "Disclose AI-generated content"
        if destination.get("disclosure_required") and rendition.get("ai_generated")
        else "No AI disclosure required by current rules"
    )
    caption = f"{title}\n\n{body}"
    if destination.get("disclosure_required") and rendition.get("ai_generated"):
        explicit_disclosure = str(
            rendition.get("ai_disclosure") or "AI-generated content."
        )
        if explicit_disclosure not in caption:
            caption = f"{caption}\n\n{explicit_disclosure}"
    files = [
        str(item)
        for item in rendition.get("media_urls", [])
        if isinstance(item, str) and item.startswith(("http://", "https://"))
    ]
    canonical_url = rendition.get("canonical_url")
    checklist = [
        "Review the destination rules before posting",
        disclosure,
        "Check title, body, media, links and language",
        "Media files: "
        + (
            ", ".join(files)
            if files
            else "none supplied; attach media manually if the destination requires it"
        ),
        "Submit link: " + url,
        "Post manually and confirm the live URL",
    ]
    if canonical_url:
        checklist.insert(3, "Canonical/content link: " + str(canonical_url))
    return {
        "files": files,
        "caption": caption,
        "deep_link": url,
        "checklist": checklist,
    }


def dispatch(
    key: str, method: str, payload: dict[str, Any], *, simulation: bool | None = None
) -> dict[str, Any]:
    man = manifest(key)
    use_sim = settings.simulation_mode if simulation is None else simulation
    if method == "validate":
        result = violations(
            payload.get("rendition", {}), payload.get("destination", {}), man
        )
        return {"ok": not result, "violations": result}
    if method == "build_assist_pack" and key == "assisted":
        return _assist(payload.get("rendition", {}), payload.get("destination", {}))
    if method == "publish":
        if key == "assisted":
            raise DistributionError(
                "METHOD_NOT_SUPPORTED",
                "Use build_assist_pack for the assisted connector.",
                status_code=405,
            )
        rendition = payload.get("rendition", {})
        supplied_dest = payload.get("destination", {})
        destination_id = supplied_dest.get("id")
        with connect() as db:
            row = (
                db.execute(
                    "SELECT * FROM destinations WHERE id=?", (destination_id,)
                ).fetchone()
                if destination_id
                else None
            )
            destination = decode_destination(row) if row else dict(supplied_dest)
            if row:
                enforce_rules(db, rendition, destination, man)
            else:
                enforce_content_rules(rendition, destination, man)
                if int(destination.get("frequency_cap", 0) or 0):
                    raise DistributionError(
                        "INVALID_REQUEST",
                        "A frequency-capped destination must be registered so its posting history can be checked.",
                        status_code=422,
                    )
        idem = str(payload.get("idempotency_key") or "")
        if not idem:
            raise DistributionError(
                "INVALID_REQUEST",
                "idempotency_key is required by the connector publish method.",
                status_code=422,
            )
        if use_sim:
            return _sim_publish(key, rendition, destination, idem)
        return _live_publish(
            key,
            rendition,
            destination,
            _connection(key, payload.get("connection", {})),
            idem,
        )
    if method == "get_status":
        pid = str(payload.get("platform_post_id", ""))
        if use_sim:
            return _sim_status(key, pid)
        dest = payload.get("destination", {})
        return _live_status(
            key, pid, dest, _connection(key, payload.get("connection", {}))
        )
    if method == "delete":
        pid = str(payload.get("platform_post_id", ""))
        if use_sim:
            return _sim_delete(key, pid)
        return _live_delete(
            key,
            pid,
            payload.get("destination", {}),
            _connection(key, payload.get("connection", {})),
        )
    if method == "fetch_metrics":
        if use_sim:
            # Simulated connectors have no engagement events; a zero count means no simulated activity recorded.
            return {
                "views": 0,
                "likes": 0,
                "comments": 0,
                "shares": 0,
                "saves": 0,
                "watch_time_s": 0,
                "clicks": 0,
            }
        return _live_metrics(
            key,
            str(payload.get("platform_post_id", "")),
            payload.get("destination", {}),
            _connection(key, payload.get("connection", {})),
            payload.get("since"),
        )
    if method == "refresh_auth":
        raise DistributionError(
            "METHOD_NOT_SUPPORTED",
            f"Auth refresh is not available for '{key}'.",
            status_code=405,
        )
    raise DistributionError(
        "METHOD_NOT_SUPPORTED",
        f"Method '{method}' is not supported by '{key}'.",
        status_code=405,
    )

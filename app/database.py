from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

from app.config import settings

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS destinations (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, url TEXT NOT NULL, platform_key TEXT NOT NULL,
  surface TEXT NOT NULL, category TEXT NOT NULL DEFAULT 'community', audience_tags TEXT NOT NULL DEFAULT '[]',
  language TEXT NOT NULL DEFAULT 'en', geo TEXT, est_audience INTEGER, rules_url TEXT,
  frequency_cap INTEGER NOT NULL DEFAULT 0, frequency_window_seconds INTEGER NOT NULL DEFAULT 604800,
  self_promo_allowed INTEGER NOT NULL DEFAULT 1, link_policy TEXT NOT NULL DEFAULT 'allowed',
  disclosure_required INTEGER NOT NULL DEFAULT 0, automation_allowed INTEGER NOT NULL DEFAULT 1,
  connector_key TEXT NOT NULL, relevance_score INTEGER NOT NULL DEFAULT 3, relevance_reason TEXT NOT NULL DEFAULT '',
  level TEXT NOT NULL DEFAULT 'L0', last_certified_at TEXT, enabled INTEGER NOT NULL DEFAULT 1,
  settings_json TEXT NOT NULL DEFAULT '{}',
  rules_confirmed INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS posts (
  id TEXT PRIMARY KEY, destination_id TEXT NOT NULL, connector_key TEXT NOT NULL, platform_post_id TEXT NOT NULL,
  live_url TEXT NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL, is_promotional INTEGER NOT NULL DEFAULT 0,
  ai_generated INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL, simulation INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL, deleted_at TEXT, idempotency_key TEXT NOT NULL,
  UNIQUE(connector_key, idempotency_key)
);
CREATE TABLE IF NOT EXISTS idempotency (
  key TEXT PRIMARY KEY, request_hash TEXT NOT NULL, job_id TEXT, response_json TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY, kind TEXT NOT NULL, status TEXT NOT NULL, progress INTEGER NOT NULL DEFAULT 0,
  request_json TEXT NOT NULL, result_json TEXT, error_json TEXT, callback_url TEXT,
  correlation_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS certifications (
  id TEXT PRIMARY KEY, destination_id TEXT NOT NULL, connector_key TEXT NOT NULL, status TEXT NOT NULL,
  evidence_json TEXT NOT NULL, created_at TEXT NOT NULL, finished_at TEXT
);
CREATE TABLE IF NOT EXISTS audit_events (
  id TEXT PRIMARY KEY, job_id TEXT, correlation_id TEXT, event_type TEXT NOT NULL,
  details_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_posts_destination_created ON posts(destination_id, created_at);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status, created_at);
"""


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    settings.ensure_directories()
    db = sqlite3.connect(settings.database_path, timeout=20, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def init_db() -> None:
    with connect() as db:
        db.executescript(SCHEMA)

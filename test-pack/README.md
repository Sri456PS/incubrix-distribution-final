# Test pack and evidence index

Run `pytest -q` from the repository root. The suite creates temporary SQLite databases and local test objects; it does not contact real destination platforms. It covers connector manifest shape and shared errors, rule violations and tailoring, idempotency, failure mapping, publication jobs, all six simulated certification flows, cleanup and failed-certification evidence, asset upload, CSV validation/deduplication and a 1,000-row performance target, health/readiness, and OpenAPI validation.

See `INDEX.md` for the test-to-requirement map. The latest passing output is in `pytest-audit-complete-run.log` and `pytest-audit-complete-results.xml`. Earlier runs, including failures that led to fixes, remain recorded. The verified run summary and limitations are in `../REPORT.md`. This folder intentionally contains no fabricated screenshots, video, deployment URL, live post, or external certification artifact.

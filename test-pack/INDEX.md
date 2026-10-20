# Test pack index

The latest retained run in `pytest-submission-run.log` and `pytest-submission-results.xml` ran 32 tests on 2026-10-19 (7.70 seconds; two dependency deprecation warnings). A later dashboard update was verified in the terminal with `python -m pytest -q -p no:cacheprovider`: 34 tests passed in 9.51 seconds with two warnings; a new log could not be saved because access to this folder was denied. Earlier failing runs are retained and one exposed the explicit-null AI disclosure bug, which was fixed and retested. Test inputs are expressed as fixtures and cases in the listed test source files; API outputs are asserted there. API/test storage uses temporary directories cleaned by pytest, so no local or provider post artifacts persist from those runs.

| Claim area | Test source | Evidence in test pack |
|---|---|---|
| Connector manifest keys, shape, and shared codes | `../tests/test_contracts.py` | Test assertions and run result; no real provider calls |
| Connector dispatch publish/status/delete contract (Discourse) and simulated certification (all six connectors) | `../tests/test_api.py` | Test assertions; simulated runs only |
| Rule limits, media, promotion, canonical-only links, AI disclosure, tailoring, relevance | `../tests/test_policy.py`; `../tests/test_api.py` | Test assertions including publication-time disclosure and direct live safety guard |
| Successful and timeout idempotency; shared provider errors; cleanup failure evidence | `../tests/test_api.py`; `../tests/test_failures.py` | Test assertions |
| CSV normalization, duplicate rows, row errors, language/relevance, 1,000-row target | `../tests/test_catalogue.py` | 1,000-row test stubs network reachability |
| Local asset storage and retrieval, health/readiness, correlation, OpenAPI, dashboard render and API-token behavior | `../tests/test_api.py` | Test assertions; S3/MinIO is not covered |
| Labeled recommendation evaluation | `../tests/test_api.py` | One labeled case; not a representative top-10 benchmark |

Missing test-pack evidence remains: durable run-by-run certification JSON/screenshots/API responses, live provider posts and deletion receipts, real reachability or enrichment measurements, callback delivery, S3/MinIO behavior, and batch end-to-end output. No claim is made for these.

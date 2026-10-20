# Assessment 4 requirement checklist

Source: `IncuBrix_Assessment_4_Distribution_Long_Tail_Destination_Network_Brief.pdf` (all 9 pages, including Appendix A). The supplied Excel Submission Form is preserved as `IncuBrix_Assessment_4_Distribution_Long_Tail_Destination_Network_Submission_Form.xlsx`. A separate evidence-filled copy is `IncuBrix_Submission_Completed.xlsx`; personal, access, and declaration fields remain for the submitter.

Status values: **Planned**, **Implemented**, **Verified**, **Partial**, **Blocked**. Evidence is recorded only after it exists.

## Must-haves (brief pages 2–3)

| ID | Requirement | Implementation target | Verification evidence | Status |
|---|---|---|---|---|
| M1 | Exact connector methods, manifest and shared error codes (Appendix A, pp. 8–9) | Six manifests and `/connector/v1/{connector_key}/{method}` dispatcher | Manifest/schema tests and simulation runs; exact parameter/result contracts are not fully tested, and adapter code writes to the distribution database | Partial |
| M2 | Certified Discourse, Lemmy, Dev.to, Ghost, signed webhook and assisted connectors | Native HTTP adapters, deterministic simulation adapters, certification lifecycle | Simulated certification tests for all six; no live provider certification | Partial |
| M3 | New destinations configured as catalogue rows | Persisted destination catalogue | Add-destination API test | Verified |
| M4 | Frequency, promotion, link and AI disclosure rules checked before every post | Persisted policy engine; block or tailor with shared reasons | Tests cover frequency, promotion, link/canonical-only restrictions, and publication-time required AI disclosure | Verified |
| M5 | No unsolicited real posts; test posts limited to local/sandbox; delete within 10 minutes; honor 429 reset | Simulation default; shared authorized-sandbox guard; cleanup tracking and rate-reset handling | Direct live connector route blocks an unapproved destination; simulation cleanup and one 429 wait test; no local-provider run or five-consecutive-failure circuit breaker | Partial |
| M6 | Same idempotency key never creates another post after timeout/retry | Durable idempotency records and job records | Retry, timeout, and job replay tests | Verified |
| M7 | Secrets absent from source/logs; full no-key simulation mode | Environment-only credentials, structured logs, simulated adapters | Secret-setting rejection and simulation tests | Verified |
| M8 | Compose starts service, local Lemmy and Ghost, plus mock servers for the rest | Compose app, storage, local platform services and independent mock servers | Compose syntax passed; no mock-server services are defined; image build/start blocked | Partial |
| M9 | Integration Standard met and live demo available | Ten points mapped below; demo deployment is external | Local checks in `REPORT.md`; live demo unavailable | Partial |

## Levels (brief pp. 3, 9)

| Level | Requirement | Evidence target | Status |
|---|---|---|---|
| Core | Every must-have; certify each connector with publish, live URL, status and delete | Six simulation records; no live post URLs | Partial |
| Core | Map expired auth, rate limit, rejected content to shared codes | Connector error mapping and tests | Verified |
| Advanced | CSV catalogue import: normalized dedupe, URL validation/reachability, row errors; 1,000 rows <5 minutes; enrichment such as member count | Import tests including 1,000-row target with reachability stubbed | Partial: no live reachability/enrichment measurement; audience count is input only |
| Advanced | AI reads a community rules page and proposes rules for human confirmation | Deterministic supplied-text proposal and explicit approval | Partial: no AI reader or rules-page crawler |
| Advanced | Relevance score 1–5 and explanation | Deterministic, documented scorer | Verified |
| Advanced | Per-destination content tailoring (discussion framing, canonical URL, word-boundary truncation, hashtag/mention style) | Validated truncation and disclosure tailoring | Partial: framing, canonical URL and hashtag/mention adaptation are absent |
| Advanced | Three additional connectors from Hashnode, Slack app, Kit/Mailchimp, MRSS, PeerTube | Optional beyond the required six | Planned |
| Outstanding | Ranked recommender and measured top-10 precision | Scorer plus evaluation endpoint; labeled data must be supplied | Partial: no evaluation score claimed |
| Outstanding | Metrics on at least three connectors | Adapters for Discourse, Lemmy, DEV.to | Partial: live calls untested |
| Outstanding | Re-test before 30 days, automatically demote/exclude failures, create ticket or alert | Scheduler/worker, demotion workflow, audit event | Partial: scheduled path is untested; no external ticket or alert integration |
| Outstanding | 150 researched real destinations and rules | Must be researched, not fabricated | Blocked pending research and evidence |

## Integration Standard (brief pp. 4–5)

| ID | Requirement | Evidence target | Status |
|---|---|---|---|
| I1 | One-command compose start/test, no keys, `.env.example` lists settings | Compose config parsed; no combined test command or successful startup | Partial |
| I2 | OpenAPI 3.1 matches service; routes under `/api/v1/` | Exported and validated `openapi.yaml`; Appendix A requires connector route `/connector/v1/...` | Partial: connector path follows Appendix A, outside the Integration Standard prefix |
| I3 | Long operations are jobs: 202, job ID, status/progress/result links; optional signed callback | Durable job API and worker | Verified |
| I4 | Idempotency-Key on every POST; repeat request returns same job | Request enforcement and persistence tests | Verified |
| I5 | JSON error envelope and documented UPPER_SNAKE_CASE codes | Error handler, error-code route, and README list | Verified for local API/docs; end-to-end error tests cover selected cases |
| I6 | All config via environment/config; no hard-coded secrets/paths | Settings module and `.env.example` | Verified |
| I7 | Upload or URL input; S3-compatible output (MinIO in Compose) | Local storage tested; MinIO configured but untested | Partial |
| I8 | JSON logs with timestamp, level, job_id, correlation ID; health/readiness | Middleware, logging and dependency checks | Verified |
| I9 | Every result has required provider/model/settings/seed/timing/cost/warnings/rights manifest | Manifest writer and publication assertions | Verified |
| I10 | Batch runner for folder/file inputs writes results to one folder | CLI implemented; help checked, end-to-end smoke test not run | Partial |

## Other Integration Standard provisions (brief p. 4)

| Provision | Status | Evidence or gap |
|---|---|---|
| Commercial-use compatibility; avoid AGPL in shipped service | Partial | Direct runtime dependencies are listed; direct/transitive dependency and container-image licenses have not all been audited |
| External AI providers behind configuration-selected adapters with a mock adapter | Partial / not used in default path | No external AI provider is used; rule proposal is deterministic and does not provide the required AI rule-page capture or provider adapter |
| Cost per unit of output | Partial | External model/provider cost for tests is zero; local compute/hosting cost and cost per 1,000 publications are unmeasured |
| Secrets only in environment or secret store | Verified in application configuration and examples | No real credentials supplied; full independent secret scan and live log review remain outstanding |

## Submission artifacts (brief pp. 5–7)

| Artifact | Plan | Status |
|---|---|---|
| README, architecture, setup, API, tests, batch runner, settings | Repository docs and executable CLI | Implemented |
| `openapi.yaml` | Export from application and validate | Verified |
| `REPORT.md` (2–4 pages) | Verified behavior, limits, measurements, cost and next steps | Implemented |
| `SOURCES.md`, `AI_USE.md` | Disclose libraries/providers and AI coding assistance | Implemented |
| Test pack and evidence index | All test inputs, outputs (including failures), logs, and index linking report claims | Test-source fixtures, actual pytest logs/JUnit reports, and test-to-requirement index exist; API outputs are asserted but temporary provider state is not retained | Partial |
| Certification pack | Per-destination run record, live URL/API response, screenshot, deletion confirmation | Simulation records are generated in temporary test storage | Partial: no retained pack; simulation is not live-provider certification |
| Catalogue files | CSVs and import reports; labeled set for recommender | Test CSV fixtures exist in test code; no researched catalogue or labeled benchmark | Blocked for the 150-destination deliverable |
| Excel Submission Form | Original preserved; evidence/status copy created with verified marks and links. User-specific fields state when details are unavailable; declaration answers/signature remain for submitter review. | Partial |
| Live demo URL, private GitHub repository, video | Require deployment/account/reviewer access or recording; not asserted from local work | Blocked |
| Demo video (unedited, at most 8 minutes) | One clean run, one failure, tests, and code/design decision | Not recorded | Blocked |

## Hard cases (brief pp. 3, 5)

Timeout + same-key retry; HTTP 429/reset; expired token; platform rejection; overlong title/body and unsupported media; frequency cap; banned promotion; API field rename causing certification failure/demotion; duplicate/bad/multilingual CSV rows; revoked partner permission; non-English content; required AI disclosure. Each must have explicit test/evidence or be marked partial.

| Hard case | Status | Current evidence or gap |
|---|---|---|
| Timeout followed by same-key retry | Verified in simulation/API wrapper | `tests/test_failures.py::test_connector_publish_timeout_is_cached_without_a_duplicate_call` |
| HTTP 429 reset | Verified at request helper | `tests/test_failures.py::test_rate_limit_wait_uses_retry_after_and_retries_once`; only one retry and no 5-failure circuit breaker |
| Expired token / rejected platform content | Partial | Shared code mappings are unit-tested; no local provider HTTP error flow |
| Overlong title, body, unsupported media | Partial | Title and media validation/tailoring tested; overlong body end-to-end case missing |
| Frequency cap / promotional post | Verified in simulated API | `tests/test_api.py::test_rule_policy_blocks_second_post_and_promotion` |
| API field change fails certification and demotes destination | Partial | Failed certification cleanup is tested; field-rename detection and previously-certified demotion are not tested end-to-end |
| Duplicate, invalid URL and mixed-language CSV | Verified with mocked reachability | `tests/test_catalogue.py`; actual network reachability is not measured |
| Partner permission revoked | Partial | 401/403 maps to expired-auth code; destination disable/owner reconnect path is not tested |
| Non-English destination | Partial | CSV/relevance path accepts language tags; non-English publication tailoring is not tested |
| Required AI disclosure | Verified for generated disclosure path | `tests/test_api.py::test_publication_adds_missing_required_ai_disclosure`; unit violation coverage also exists |

## Audit notes from the current implementation

- The latest run in this workspace passed 34 tests (`python -m pytest -q -p no:cacheprovider`); its output was terminal-only because writes to `test-pack/` were denied. The retained 32-test logs are historical. Tests cover simulation paths and are not live provider verification.
- Contract tests validate manifest shape and shared codes; full request/response field tests for every connector method remain needed. Connector adapters also write to the same SQLite `posts` table used by the service runtime, contrary to Appendix A's separation requirement.
- The 1,000-row timing test stubs reachability, so it measures parsing and persistence only. `est_audience` is accepted as input; member-count enrichment is not implemented.
- No Compose service currently provides the explicit mock HTTP servers requested for connectors other than local Lemmy/Ghost.
- The original workbook has now been inspected and the required workbook rows are mapped in the separate evidence copy. Blank personal/declaration fields are intentionally not invented. Required workbook claims: each must-have/level/Integration Standard item marked Yes/Partial/No with evidence link; measured numbers and hard-case outcomes; Metrics tab for certification counts/durations/evidence, simulation/local reliability and duplicate counts, rule catches/misses, catalogue counts and 1,000-row time, labeled top-10 precision, and cost per 1,000 publications; Hard cases tab with each scenario and result.
- The PDF's Submission Form instructions also call for an unedited demo video (maximum eight minutes), a private GitHub repository with real commit history, a live demo URL, and a test pack containing real inputs, outputs, logs, and evidence index. These have not been fabricated or claimed.

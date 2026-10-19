# Assessment 4 implementation and test report

**Run date:** 2026-10-19  
**Source brief:** `IncuBrix_Assessment_4_Distribution_Long_Tail_Destination_Network_Brief.pdf`  
**Scope:** Local project implementation; no live community posting or external deployment was performed.

## Delivered

Implemented a Python/FastAPI service with six Appendix A connectors, YAML manifests, a SQLite destination/job/idempotency/certification store, filesystem and S3-compatible object storage, policy enforcement, CSV import, background publication/certification jobs, deterministic recommendation/evaluation, rules proposals, batch execution, Docker Compose services, health/readiness, JSON request logging and OpenAPI 3.1.

Simulation is the default. Automated certification records simulation evidence and attempts cleanup after a known publish. This evidence is not a platform certification. Native calls require user-configured credentials and authorized sandbox destinations; they were not exercised in this run.

## Approach decisions

Python 3.12/FastAPI follows the brief's Python 3.11+ connector option and gives the runtime a single HTTP/OpenAPI surface. SQLite keeps the local job and catalogue setup self-contained; storage is selectable between local files and S3-compatible object storage. The default path uses deterministic, no-key simulation and ranking, avoiding paid model calls and unlicensed media. These choices make the local API testable, but in-process simulation is not a substitute for the brief's separate mock servers or its requirement that connector methods return results without writing the IncuBrix database.

## Verification performed

| Check | Result |
|---|---|
| `pytest -q --junitxml=test-pack/pytest-submission-results.xml` | **32 passed** in 7.70s; two dependency deprecation warnings; console and JUnit output saved in `test-pack/pytest-submission-run.log` and `test-pack/pytest-submission-results.xml` |
| `ruff check app tests scripts` | **Passed** |
| `ruff format --check app tests scripts` | **Passed** after formatting |
| OpenAPI export | `python -m scripts.export_openapi` generated `openapi.yaml` |
| OpenAPI validation | `openapi-spec-validator` accepted the exported document |
| `docker compose config --quiet` | Exit code 0; Docker emitted an access-denied warning reading the per-user `C:\Users\RJ\.docker\config.json` |
| Docker read-only diagnostics | Client 29.8.2 is installed. `docker version` in the sandbox reports permission denied on `npipe:////./pipe/docker_engine`; `docker buildx ls` reports `CreateFile C:\Users\RJ\.docker\buildx\instances: Access is denied.` A read-only elevated ACL check shows `SRIRAM\RJ` has Full Control on the Buildx instances path, so that denial is from the Codex sandbox boundary, not the file ACL. Windows reports `com.docker.service` Stopped/Manual while Docker backend/build processes exist. An elevated `docker version` produced no output for 33 seconds and was interrupted; engine health remains unverified. |
| `docker compose build api` | Blocked: after the initial “Image ... Building” message, Docker produced no progress or error for over five minutes; I interrupted it. No ACL or Docker configuration was changed. |
| Full stack startup | Not run; image build and local service account bootstrap are not yet verified |

During this audit, regression tests exposed and fixed four code issues: direct connector publication did not share the authorized-sandbox guard used by background jobs; direct publish could skip content rules for an unregistered destination; a missing AI disclosure became the literal string `None` because the API model passes an explicit null; and canonical-only link policy was accepted but not enforced. Earlier failing test runs remain in the test pack; the latest recorded run passes.

The CSV suite includes a 1,000-row import performance test with reachability stubbed to keep the test deterministic. The test asserts completion under five minutes; this is not a benchmark of live network reachability.

## Remaining gaps and limitations

- The supplied Excel workbook is preserved unchanged. `IncuBrix_Submission_Completed.xlsx` is a separate evidence/status copy; personal contact details, demo/repository links, declaration answers, signature, and date remain blank for the submitter.
- The workbook copy retains the original sheets, formulas, validation rules, and formatting. Marks are limited to verified implementation/tests; simulated certification is labeled, and unavailable metrics remain explicitly unmeasured.
- Docker's Buildx ACL is healthy for the Windows account; the Codex process is sandbox-restricted from the C: profile and Docker named pipe. Do not change ACLs or delete `.docker` configuration to address this Codex-only access boundary. Docker Desktop itself also reports its Windows service as stopped/manual, so confirm Docker Desktop's engine status in the UI or test from the working VS Code terminal.
- The required 150 researched real destinations and supporting rule evidence are not included. Sample/test catalogue rows do not satisfy that research deliverable.
- Live demo URL, private hosted Git repository, and video are external submission tasks and are not available from local verification.
- Docker Compose syntax parsed, but the image build stalled and was interrupted. The current Codex process cannot read the Buildx directory or access the Docker Engine pipe; service startup and local Lemmy/Ghost account bootstrap remain unverified. This evidence does not establish whether the engine itself is healthy outside the restricted process.
- External native connector requests have not been sent. Provider credentials, target sandboxes, and provider-specific setup are required to verify those paths end to end.
- Rule proposal uses deterministic phrase matching of supplied text, not an AI rules-page crawler/model. Recommendation evaluation requires a supplied labeled set; no precision value is claimed without one.
- Three metrics adapters exist for Discourse, Lemmy, and DEV.to, but live provider metrics have not been tested.
- No advanced connector beyond the six core connectors is implemented.
- The dependency source list exists, but commercial-use license compatibility has not been audited for every direct and transitive dependency or container image.
- Important test gaps: connector request/response contract for all methods and all connectors; callback signature/delivery; local native provider HTTP flows; S3/MinIO storage and failure paths; restart/recovery and certification demotion; rate-limit circuit breaker after five consecutive failures; API-level link-policy enforcement; real (non-stubbed) reachability/enrichment; and the batch runner end-to-end.
- Recorded metrics remain incomplete for the submission workbook: no local/live connector success rates, aggregate certification times, rule catch/miss totals, labeled top-10 score, or retained 1,000-row timing value have been produced. Simulation invokes no external API/model charge; its local compute and hosting costs have not been measured, so no cost-per-1,000 figure is claimed.

## Cost and next steps

No external provider/model calls were made, so external API/model charges for this test run were zero. Local compute and hosting costs are unmeasured; a cost per 1,000 publications cannot yet be reported. Next, complete the workbook?s personal/access/declaration fields, provide destination research evidence, then authorize and configure a local provider sandbox. Build and start Compose, bootstrap test accounts, run connector certification in those sandboxes, and capture genuine run artifacts. Deploy only with explicit hosting/access authorization.

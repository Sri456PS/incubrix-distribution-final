# Sources and dependencies

## Assessment

- `IncuBrix_Assessment_4_Distribution_Long_Tail_Destination_Network_Brief.pdf` is the requirements source and is preserved in the repository.
- The supplied Excel Submission Form is preserved unchanged; `IncuBrix_Submission_Completed.xlsx` is the separate evidence-filled copy.

## Runtime dependencies

Exact allowed versions/ranges are in `requirements.txt` and `requirements-dev.txt`.

- FastAPI and Uvicorn: HTTP API and ASGI server.
- Pydantic: request validation.
- HTTPX: outbound connector and reachability requests.
- PyYAML: connector manifests and OpenAPI export.
- boto3: S3-compatible object storage.
- python-multipart: asset upload handling.
- SQLite: Python standard library persistence.
- `pypdf` 6.21.1 was installed only in a temporary directory to extract/read the supplied PDF during this audit; it is not an application dependency and is not listed in project requirements.

The local Python package metadata identifies FastAPI as MIT, Uvicorn as BSD-3-Clause, `python-multipart` and `openapi-spec-validator` as Apache-2.0, and Ruff as MIT. Some installed package metadata did not expose a normalized license expression during this audit; verify every direct and transitive package license before claiming commercial-use clearance.

## Platform references

- DEV.to/Forem API documentation: https://developers.forem.com/api/v1
- Other platform adapter request formats are implemented from their documented API conventions; run only against locally controlled or explicitly authorized test instances.

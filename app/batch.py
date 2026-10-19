from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

import httpx


def run_batch(
    input_path: Path, output_dir: Path, api_base_url: str | None = None
) -> list[dict[str, Any]]:
    base = (api_base_url or os.getenv("API_BASE_URL", "http://localhost:8000")).rstrip(
        "/"
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    files = [input_path] if input_path.is_file() else sorted(input_path.glob("*.json"))
    results: list[dict[str, Any]] = []
    with httpx.Client(timeout=30) as client:
        for input_file in files:
            record: dict[str, Any] = {"input": input_file.name}
            try:
                data = json.loads(input_file.read_text(encoding="utf-8"))
                digest = hashlib.sha256(
                    input_file.read_bytes()
                    + str(data.get("destination_id", "")).encode()
                ).hexdigest()
                response = client.post(
                    f"{base}/api/v1/publications",
                    json=data,
                    headers={"Idempotency-Key": f"batch-{digest}"},
                )
                response.raise_for_status()
                job = response.json()
                deadline = time.monotonic() + 300
                while time.monotonic() < deadline:
                    state = client.get(f"{base}{job['status_url']}")
                    state.raise_for_status()
                    status = state.json()
                    if status["status"] in {"succeeded", "failed"}:
                        record["job"] = status
                        if status["status"] == "succeeded":
                            result = client.get(
                                f"{base}/api/v1/jobs/{job['job_id']}/result"
                            )
                            result.raise_for_status()
                            record["result"] = result.json()
                        break
                    time.sleep(0.2)
                else:
                    record["error"] = {
                        "code": "PLATFORM_UNAVAILABLE",
                        "message": "Timed out waiting for job.",
                    }
            except (
                httpx.HTTPError,
                json.JSONDecodeError,
                OSError,
                KeyError,
                ValueError,
            ) as exc:
                record["error"] = {
                    "code": "BATCH_INPUT_OR_API_ERROR",
                    "message": str(exc),
                }
            (output_dir / f"{input_file.stem}.result.json").write_text(
                json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            results.append(record)
    index = {
        "count": len(results),
        "results": [f"{Path(item['input']).stem}.result.json" for item in results],
    }
    (output_dir / "index.json").write_text(
        json.dumps(index, indent=2), encoding="utf-8"
    )
    return results


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run JSON publication inputs through IncuBrix Distribution."
    )
    parser.add_argument("input", type=Path, help="JSON file or directory of JSON files")
    parser.add_argument(
        "output", type=Path, help="Directory for result records and index"
    )
    parser.add_argument(
        "--api",
        default=None,
        help="API base URL (defaults to API_BASE_URL or localhost:8000)",
    )
    args = parser.parse_args()
    results = run_batch(args.input, args.output, args.api)
    print(
        json.dumps(
            {"processed": len(results), "output_dir": str(args.output.resolve())}
        )
    )


if __name__ == "__main__":
    main()

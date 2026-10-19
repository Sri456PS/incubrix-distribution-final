from __future__ import annotations

from pathlib import Path

import yaml

from app.main import app

root = Path(__file__).resolve().parents[1]
target = root / "openapi.yaml"
target.write_text(
    yaml.safe_dump(app.openapi(), sort_keys=False, allow_unicode=True), encoding="utf-8"
)
print(f"Wrote {target}")

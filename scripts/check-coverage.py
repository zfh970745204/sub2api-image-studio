from __future__ import annotations

import json
import sys
from pathlib import Path


QUALITY_SCOPE_MINIMUM = 80.0
CORE_MINIMUM = 90.0
CORE_FILES = {
    "app/catalog.py",
    "app/config.py",
    "app/repositories/models.py",
    "app/services/rbac.py",
    "app/storage.py",
}


def normalized(path: str) -> str:
    return path.replace("\\", "/")


def percent(covered: int, statements: int) -> float:
    return 100.0 if statements == 0 else covered / statements * 100


def main() -> int:
    report_path = Path(sys.argv[1] if len(sys.argv) > 1 else "backend/coverage.json")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    files = {normalized(name): payload for name, payload in report["files"].items()}
    missing = sorted(CORE_FILES - files.keys())
    if missing:
        raise SystemExit(f"coverage report is missing core files: {', '.join(missing)}")

    totals = report["totals"]
    quality_scope = percent(totals["covered_lines"], totals["num_statements"])
    core_covered = sum(files[name]["summary"]["covered_lines"] for name in CORE_FILES)
    core_statements = sum(files[name]["summary"]["num_statements"] for name in CORE_FILES)
    core = percent(core_covered, core_statements)
    print(f"quality coverage: scope={quality_scope:.2f}% core={core:.2f}%")
    if quality_scope < QUALITY_SCOPE_MINIMUM or core < CORE_MINIMUM:
        print(
            "required coverage: "
            f"scope>={QUALITY_SCOPE_MINIMUM:.0f}% core>={CORE_MINIMUM:.0f}%",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

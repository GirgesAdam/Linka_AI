from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path


def _document(path: Path) -> dict[str, str]:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        document_format = "csv"
    elif suffix == ".xlsx":
        document_format = "xlsx"
    else:
        raise SystemExit(f"Unsupported import file: {path}. Use .csv or .xlsx.")
    return {
        "name": path.name,
        "format": document_format,
        "content_base64": base64.b64encode(path.read_bytes()).decode("ascii"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build a Linka clinic tabular-import JSON payload from CSV/XLSX files."
    )
    parser.add_argument("--mapping", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--confirm-no-existing-appointments",
        action="store_true",
        help="Use only when the clinic confirms there are no existing appointments to import.",
    )
    parser.add_argument("files", nargs="+", type=Path)
    args = parser.parse_args()

    mapping = json.loads(args.mapping.read_text(encoding="utf-8"))
    payload = {
        "documents": [_document(path) for path in args.files],
        "mapping": mapping,
        "confirm_no_existing_appointments": args.confirm_no_existing_appointments,
    }
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

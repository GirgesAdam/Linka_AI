from __future__ import annotations

import os
import sys


def credential_is_configured(value: str | None) -> bool:
    return isinstance(value, str) and bool(value.strip())


def main() -> int:
    if not credential_is_configured(
        os.environ.get("PRODUCTION_MIGRATION_DATABASE_URL")
    ):
        print(
            "::error::PRODUCTION_MIGRATION_DATABASE_URL is missing; production migration is blocked.",
            file=sys.stderr,
        )
        return 1

    print("Production migration credential is configured; its value is not displayed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

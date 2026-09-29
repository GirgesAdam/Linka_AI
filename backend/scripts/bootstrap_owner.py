import sys


def main() -> int:
    print(
        "This script has been retired because Linka now uses admin/member roles only. "
        "Use: python scripts/bootstrap_admin.py ...",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

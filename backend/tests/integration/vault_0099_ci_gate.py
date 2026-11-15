"""Fail-closed exact pytest count check for mandatory Vault PostgreSQL CI suites."""
from __future__ import annotations

import sys
from pathlib import Path
from xml.etree import ElementTree


def totals(path: Path) -> dict[str, int]:
    root = ElementTree.parse(path).getroot()
    suites = root.findall("testsuite") if root.tag == "testsuites" else [root]
    if not suites:
        raise ValueError("No JUnit suites found")
    return {
        key: sum(int(suite.get(key, "0")) for suite in suites)
        for key in ("tests", "failures", "errors", "skipped")
    }


def validate(path: Path, count: int) -> dict[str, int]:
    expected = {"tests": count, "failures": 0, "errors": 0, "skipped": 0}
    actual = totals(path)
    if actual != expected:
        raise RuntimeError(f"Mandatory PG17 test gate mismatch: expected={expected} actual={actual}")
    return actual


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("Usage: vault_0099_ci_gate.py <junit.xml> <expected-positive-count>")
    count = int(sys.argv[2])
    if count <= 0:
        raise SystemExit("Test count must be positive")
    result = validate(Path(sys.argv[1]), count)
    print(f"VAULT_POSTGRES_17_CI_GATE: {result['tests']} PASS, 0 skipped, 0 failures")

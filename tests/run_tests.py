"""Minimal test runner (no third-party dependencies).

Usage:
    python tests/run_tests.py            # run every test module
    python tests/run_tests.py -k budget  # filter by name substring
    python tests/run_tests.py -v         # per-test results
"""

from __future__ import annotations

import importlib
import os
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

TEST_MODULES = [
    "test_api_restrictions",
    "test_source_policy",
    "test_happy_path",
    "test_budget",
    "test_idempotency",
    "test_supervisor",
    "test_evidence",
    "test_projection_leakage",
    "test_build_failure",
    "test_oracle_privacy",
    "test_http_server",
]


def _iter_tests(module) -> list[tuple[str, object]]:
    out = []
    for name in sorted(dir(module)):
        if not name.startswith("test_"):
            continue
        fn = getattr(module, name)
        if callable(fn):
            out.append((f"{module.__name__}::{name}", fn))
    return out


def main(argv: list[str]) -> int:
    verbose = "-v" in argv or "--verbose" in argv
    keyword = None
    if "-k" in argv:
        keyword = argv[argv.index("-k") + 1]

    modules = []
    for name in TEST_MODULES:
        try:
            modules.append(importlib.import_module(name))
        except Exception:
            print(f"IMPORT FAILURE in {name}:")
            traceback.print_exc()
            return 1

    selected: list[tuple[str, object]] = []
    for module in modules:
        selected.extend(_iter_tests(module))
    if keyword:
        selected = [(n, f) for n, f in selected if keyword in n]

    passed = failed = 0
    failures: list[tuple[str, str]] = []
    for name, fn in selected:
        try:
            fn()
        except Exception:
            failed += 1
            detail = traceback.format_exc()
            failures.append((name, detail))
            print(f"FAIL {name}")
            if verbose:
                print(detail)
        else:
            passed += 1
            if verbose:
                print(f"ok   {name}")
    print(f"\n{passed} passed, {failed} failed (of {len(selected)} selected)")
    for name, detail in failures:
        print("\n" + "=" * 72)
        print(f"FAILURE: {name}")
        print(detail.rstrip())
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

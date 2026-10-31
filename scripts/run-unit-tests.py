"""Run the unit tests and retry only the failed ones.

GitHub Actions has no built-in step retry. This runner runs the whole suite
once, then re-runs only the tests that failed, each attempt in a fresh
process, up to --attempts runs in total. A test that passes on a retry is
reported as flaky (a GitHub warning annotation); a test that fails every
attempt fails the step.

    python scripts/run-unit-tests.py [--attempts 3]
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS = os.path.join(ROOT, "tests")
HOLDER = re.compile(r"^\w+ \((.+)\)$")  # "setUpClass (module.Class)"


def _loadable(test_id):
    m = HOLDER.match(test_id)
    return m.group(1) if m else test_id


def run_once(names, out):
    sys.path.insert(0, TESTS)
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromNames(names) if names else loader.discover(TESTS, top_level_dir=TESTS)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    failed = [t.id() for t, _ in result.failures + result.errors] + [t.id() for t in result.unexpectedSuccesses]
    with open(out, "w", encoding="utf-8") as f:
        json.dump(sorted({_loadable(i) for i in failed}), f)
    return 0 if result.wasSuccessful() else 1


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--attempts", type=int, default=3)
    p.add_argument("--once", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--out", help=argparse.SUPPRESS)
    p.add_argument("names", nargs="*", help=argparse.SUPPRESS)
    a = p.parse_args()
    if a.once:
        return run_once(a.names, a.out)

    names, first_failed = [], None
    fd, out = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    try:
        for attempt in range(1, max(1, a.attempts) + 1):
            if attempt > 1:
                print(f"\n=== Attempt {attempt}/{a.attempts}: retrying {len(names)} failed test(s) ===", flush=True)
                for n in names:
                    print(f"  {n}", flush=True)
            code = subprocess.run([sys.executable, os.path.abspath(__file__), "--once", "--out", out, *names], cwd=ROOT).returncode
            try:
                with open(out, encoding="utf-8") as f:
                    failed = json.load(f)
            except (OSError, ValueError):
                failed = None
            if code == 0:
                for n in first_failed or []:
                    print(f"::warning title=Flaky test::{n} failed on attempt 1 and passed on attempt {attempt}", flush=True)
                return 0
            if not failed:  # crashed without a usable list: nothing safe to retry
                return code
            if first_failed is None:
                first_failed = failed
            names = failed
        for n in names:
            print(f"::error title=Test failed {a.attempts} times::{n}", flush=True)
        return 1
    finally:
        os.remove(out)


if __name__ == "__main__":
    sys.exit(main())

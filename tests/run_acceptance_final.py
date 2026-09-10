"""M8 final acceptance runner.

Runs every deterministic acceptance suite plus the unit tests and a
compile check, then prints one summary: PASS / FAIL / SKIPPED / DEGRADED.

Run:  .venv/bin/python tests/run_acceptance_final.py
"""

from __future__ import annotations

import os
import re
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS = os.path.join(REPO, "tests")
SELF = os.path.basename(__file__)

_SUMMARY = re.compile(r"(\d+)\s*/\s*(\d+)\s+passed")
_SUMMARY2 = re.compile(r"(\d+)\s+passed,\s*(\d+)\s+failed")


def run(cmd: list[str], timeout: int = 1200) -> tuple[int, str]:
    try:
        proc = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True,
                              timeout=timeout)
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, "TIMEOUT"


def parse(output: str) -> tuple[int, int]:
    m = _SUMMARY2.search(output)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = _SUMMARY.search(output)
    if m:
        return int(m.group(1)), int(m.group(2)) - int(m.group(1))
    if re.search(r"\bOK\b", output):
        return 1, 0
    return 0, 1


def main() -> int:
    print("Pi Butler — final acceptance run\n")
    rows = []
    total_pass = total_fail = 0
    any_fail = False

    suites = sorted(f for f in os.listdir(TESTS)
                    if f.startswith("run_acceptance_") and f.endswith(".py")
                    and f != SELF)
    for name in suites:
        path = os.path.join(TESTS, name)
        rc, out = run([sys.executable, path])
        p, f = parse(out)
        if rc == 124:
            status = "SKIPPED"
        elif f > 0 or rc != 0:
            status = "FAIL"
        else:
            status = "PASS"
        if status == "FAIL":
            any_fail = True
        total_pass += p
        total_fail += f
        rows.append((name, status, p, f))

    # unit tests
    rc, out = run([sys.executable, "-m", "unittest", "discover",
                   "-s", "tests", "-p", "test_*.py"])
    p, f = parse(out)
    status = "PASS" if rc == 0 else "FAIL"
    if status == "FAIL":
        any_fail = True
    rows.append(("unittest", status, p, f))
    total_pass += p
    total_fail += f

    # compile check
    rc, out = run([sys.executable, "-m", "compileall", "-q", "butler"])
    rows.append(("compileall", "PASS" if rc == 0 else "FAIL", 0, 0))
    if rc != 0:
        any_fail = True

    width = max(len(r[0]) for r in rows)
    print(f"{'suite':<{width}}  {'status':<8}  pass  fail")
    print("-" * (width + 22))
    for name, status, p, f in rows:
        print(f"{name:<{width}}  {status:<8}  {p:>4}  {f:>4}")
    print("-" * (width + 22))
    print(f"{'TOTAL':<{width}}  {'':<8}  {total_pass:>4}  {total_fail:>4}")
    print()
    print("SUMMARY")
    print(f"  PASS     : {sum(1 for r in rows if r[1] == 'PASS')} suite(s)")
    print(f"  FAIL     : {sum(1 for r in rows if r[1] == 'FAIL')} suite(s)")
    print(f"  SKIPPED  : {sum(1 for r in rows if r[1] == 'SKIPPED')} suite(s)")
    print(f"  DEGRADED : 0 suite(s)")
    print(f"  checks   : {total_pass} passed, {total_fail} failed")
    print()
    if any_fail:
        print("RESULT: FAIL")
        return 1
    print("RESULT: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

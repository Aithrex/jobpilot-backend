"""No module may use a name it never defined or imported.

a18db94 added a deep-crawl branch to `job_ingest_worker._list_career` that
called `_html_to_text` without importing it. The offline suite was green -
4,900 tests, none of which ran that branch - and CI deployed it. In
production every Greenhouse board raised NameError on every deep run: for a
day, no aged early-career role was admitted from 686 of the 1,309 career-page
companies, and the only trace was "errors: 30" in each run record.

A test that runs the branch now exists. This is the guard for the next
branch nobody writes a test for.
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_no_undefined_names():
    out = subprocess.run(
        [sys.executable, "-m", "ruff", "check", "--select", "F821,F823",
         "--no-cache", "--output-format", "concise",
         "shared/python", "services", "scripts", "local.py"],
        cwd=str(ROOT), capture_output=True, text=True)
    assert out.returncode == 0, (
        "undefined names (run `python -m ruff check --select F821,F823 "
        "shared/python services scripts`):\n" + out.stdout + out.stderr)

"""The CI / pre-push entry point must stay wired to the offline suite."""
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def test_verify_help_and_fast_default_selectors():
    script = ROOT / "scripts" / "verify.py"
    assert script.is_file()
    help_run = subprocess.run(
        [sys.executable, str(script), "--help"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert help_run.returncode == 0
    assert "--full" in help_run.stdout
    assert "pytest tests evals" in help_run.stdout


def test_tests_workflow_invokes_verify_full():
    text = (ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")
    assert "python scripts/verify.py --full" in text


def test_deploy_refuses_failed_or_missing_suite():
    text = (ROOT / ".github" / "workflows" / "deploy.yml").read_text(encoding="utf-8")
    assert "refusing to deploy" in text
    assert "REQUIRE_GREEN_TESTS" not in text
    assert 'head_sha=${GITHUB_SHA}' in text
    assert "workflow_dispatch is not a bypass" in text or "not a bypass" in text

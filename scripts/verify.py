#!/usr/bin/env python3
"""CI-equivalent verification for jobpilot-backend.

This is the one entry point local development, pre-push hooks, and
`.github/workflows/tests.yml` share.

Usage (from the backend repo root):

  python scripts/verify.py --fast [pytest selectors...]
      Change-focused checks. Default selectors cover renderer density,
      pagination, and layout if none are given.

  python scripts/verify.py --full
      Offline suite matching CI: PYTHONPATH=shared/python pytest tests evals -q
      ENVIRONMENT=development USE_SSM_SECRETS=false. No production
      credentials or paid model calls.

  python scripts/verify.py --full --cache
      Same as --full, but reuse a successful stamp when HEAD, the working
      tree, requirements, and Python version are unchanged.

Python 3.12 is required (Lambda / CI). Fonts are the bundled Charis SIL
files under shared/python/services/fonts — the same files CI uses.
Rendering page counts can still differ if a machine lacks those fonts and
falls back to Times; use scripts/verify-linux.sh when you need the Ubuntu
CI environment.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FAST = [
    "tests/test_renderer_density.py",
    "tests/test_two_page.py",
    "tests/test_resume_layout.py",
    "tests/test_pagination_education_orphan.py",
    "tests/test_content_selection.py",
]
CACHE_NAME = "verify-full.stamp"
REQUIRED_MAJOR, REQUIRED_MINOR = 3, 12


def _stamp_path() -> Path:
    git_dir = ROOT / ".git"
    if git_dir.is_file():
        # worktree: .git is a file pointing at the real git dir
        try:
            text = git_dir.read_text(encoding="utf-8")
            for line in text.splitlines():
                if line.startswith("gitdir:"):
                    return Path(line.split(":", 1)[1].strip()) / CACHE_NAME
        except OSError:
            pass
    return git_dir / CACHE_NAME


def _run(cmd: list[str], env: dict[str, str]) -> int:
    print("+", " ".join(cmd), flush=True)
    return subprocess.call(cmd, cwd=str(ROOT), env=env)


def _fingerprint() -> dict[str, str]:
    def sha_file(path: Path) -> str:
        h = hashlib.sha256()
        h.update(path.read_bytes())
        return h.hexdigest()

    reqs = []
    for rel in ("shared/python/requirements.txt", "requirements-dev.txt"):
        path = ROOT / rel
        if path.is_file():
            reqs.append(f"{rel}:{sha_file(path)}")

    head = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=str(ROOT), text=True
    ).strip()
    diff = subprocess.check_output(
        ["git", "diff", "HEAD"], cwd=str(ROOT)
    )
    untracked = subprocess.check_output(
        ["git", "ls-files", "--others", "--exclude-standard"],
        cwd=str(ROOT),
    )
    tree = hashlib.sha256(diff + b"\n" + untracked).hexdigest()
    return {
        "head": head,
        "tree": tree,
        "python": sys.version.split()[0],
        "requirements": "|".join(reqs),
        "suite": "tests+evals",
    }


def _cache_valid() -> bool:
    path = _stamp_path()
    if not path.is_file():
        return False
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return saved.get("ok") is True and saved.get("fp") == _fingerprint()


def _write_cache(ok: bool) -> None:
    path = _stamp_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"ok": ok, "fp": _fingerprint()}, indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        print(f"verify: could not write stamp ({exc})", file=sys.stderr)


def _check_python() -> None:
    if sys.version_info[:2] != (REQUIRED_MAJOR, REQUIRED_MINOR):
        print(
            f"verify: Python {REQUIRED_MAJOR}.{REQUIRED_MINOR}.x is required "
            f"(CI / Lambda). This interpreter is {sys.version.split()[0]}.",
            file=sys.stderr,
        )
        sys.exit(2)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--fast",
        action="store_true",
        help="Change-focused pytest selectors (default if extra args given)",
    )
    mode.add_argument(
        "--full",
        action="store_true",
        help="CI offline suite: pytest tests evals -q",
    )
    parser.add_argument(
        "--cache",
        action="store_true",
        help="Reuse a successful --full stamp when the relevant state is unchanged",
    )
    parser.add_argument(
        "pytest_args",
        nargs=argparse.REMAINDER,
        help="Extra pytest args (use after --). Leading -- is stripped.",
    )
    args = parser.parse_args(argv)

    extra = list(args.pytest_args)
    if extra and extra[0] == "--":
        extra = extra[1:]

    use_full = args.full or (not args.fast and not extra)
    if args.fast and use_full:
        use_full = False

    _check_python()

    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "shared" / "python")
    env.setdefault("ENVIRONMENT", "development")
    env.setdefault("USE_SSM_SECRETS", "false")

    if use_full and args.cache and _cache_valid():
        print(
            "verify: reusing successful --full stamp "
            f"(HEAD {_fingerprint()['head'][:7]}, tree unchanged)"
        )
        return 0

    if use_full:
        cmd = [sys.executable, "-m", "pytest", "tests", "evals", "-q", *extra]
    else:
        selectors = extra or DEFAULT_FAST
        cmd = [sys.executable, "-m", "pytest", *selectors, "-q"]

    rc = _run(cmd, env)
    if use_full:
        _write_cache(rc == 0)
    if rc != 0:
        print(
            "verify: FAILED. Do not declare the change ready or deploy. "
            "Do not skip, weaken, or suppress this check to obtain a green result.",
            file=sys.stderr,
        )
    return rc


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Install a repository-local pre-push hook that runs scripts/verify.py --full.

Does not change global Git configuration. Copies the hook into this repo's
.git/hooks/pre-push (or the worktree git dir). CI remains authoritative;
the hook can be bypassed with --no-verify and is supplementary.

Usage (from the backend repo root):

  python scripts/install_pre_push_hook.py
"""
from __future__ import annotations

import stat
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOOK_SOURCE = ROOT / ".githooks" / "pre-push"


def _git_dir() -> Path:
    git = ROOT / ".git"
    if git.is_dir():
        return git
    if git.is_file():
        for line in git.read_text(encoding="utf-8").splitlines():
            if line.startswith("gitdir:"):
                return Path(line.split(":", 1)[1].strip())
    raise SystemExit("install_pre_push_hook: .git not found; run from the backend repo")


def main() -> int:
    if not HOOK_SOURCE.is_file():
        print(f"missing {HOOK_SOURCE}", file=sys.stderr)
        return 1
    dest = _git_dir() / "hooks" / "pre-push"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(HOOK_SOURCE.read_bytes())
    dest.chmod(dest.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    print(f"Installed repository-local pre-push hook at {dest}")
    print("Pushes will run: python scripts/verify.py --full --cache")
    print("CI remains the authoritative gate. Bypass only with git push --no-verify.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

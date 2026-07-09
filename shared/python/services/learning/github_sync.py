"""Learning §6.6 step 6 — push a completed build to GitHub.

Two responsibilities, both integrity-bounded:

  * `ensure_task_repo(user_email, path_id, task_ref)` — if the user has the
    GitHub "Connect & act" integration linked (``github_connect_service``, scope
    ``public_repo``), CREATE/SYNC a public repo named for the task and return its
    URL. Otherwise return the EXACT manual `git` steps so the user can publish it
    themselves. We only ever create the (empty) repo — the consented act the user
    triggered from the UI — and NEVER push their files on their behalf beyond the
    already-consented ``publish_project`` flow. The user's own code push stays in
    their hands.

  * `push_nudges(task)` — concrete, task-SPECIFIC coaching ("commit the failing
    test first", "add a README explaining the tradeoff on X"), derived in CODE
    from the task's proof_type + acceptance_criteria (no LLM, no fabrication).

This module lives in the shared layer, so the domain-local
``github_connect_service`` (it lives under ``services/resume/services/``) is
imported LAZILY inside functions and degrades to manual instructions if it isn't
importable in the current deployment (e.g. the extracted learning Lambda).
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from utils.db_connect import DBConnect

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _db():
    return DBConnect().get_db()


def _now():
    return datetime.now(timezone.utc)


def _github_connect():
    """Lazy handle to the domain-local GitHub connect service. Returns None when
    it isn't on the path (shared layer must not hard-depend on a domain service)."""
    try:
        from services import github_connect_service as gh  # domain-local (resume)
        return gh
    except Exception:  # pragma: no cover - absent in some deployments
        return None


def _slug(s: str, maxlen: int = 40) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")
    return (s[:maxlen].strip("-")) or "project"


def _find_task(path: Dict, task_ref: str) -> Optional[Dict]:
    """Locate a task on a learning path (canonical `tasks`, then legacy `modules`
    mirror). Mirrors learning_verifier._find_task without importing a private name."""
    for t in path.get("tasks") or []:
        if t.get("task_ref") == task_ref:
            return t
    for m in path.get("modules") or []:
        if m.get("task_ref") == task_ref:
            return {
                "task_ref": task_ref,
                "brief": m.get("objective") or m.get("title") or "",
                "proof_type": m.get("proof_type"),
                "acceptance_criteria": m.get("acceptance_criteria") or [],
                "constraints": m.get("concepts") or [],
            }
    return None


def _load_task(user_email: str, path_id: str, task_ref: str) -> Tuple[Dict, Dict]:
    path = _db().learning_paths.find_one({"user_email": user_email, "path_id": path_id})
    if not path:
        raise ValueError("Learning path not found")
    task = _find_task(path, task_ref)
    if not task:
        raise ValueError("Task not found on this path")
    return path, task


def _repo_name(path: Dict, task_ref: str) -> str:
    gap = str(path.get("topic") or (path.get("differentiator") or {}).get("gap") or "project")
    return f"jobpilot-{_slug(gap, 30)}-{_slug(task_ref, 20)}"


def _repo_description(path: Dict, task: Dict) -> str:
    gap = str(path.get("topic") or (path.get("differentiator") or {}).get("gap") or "").strip()
    brief = str(task.get("brief") or "").strip()
    base = f"Proof-of-work for \"{gap}\"" if gap else "JobPilot learning proof-of-work"
    return (f"{base} — {brief}" if brief else base)[:340]


# ---------------------------------------------------------------------------
# Manual push instructions (deterministic, no fabrication)
# ---------------------------------------------------------------------------
def _push_instructions(repo_url: Optional[str], default_branch: str,
                       *, connected: bool, repo_name: str = "") -> List[str]:
    branch = default_branch or "main"
    if connected and repo_url:
        remote = repo_url.rstrip("/")
        remote = remote if remote.endswith(".git") else remote + ".git"
        return [
            "git init",
            "git add .",
            'git commit -m "Initial commit — proof of work"',
            f"git branch -M {branch}",
            f"git remote add origin {remote}",
            f"git push -u origin {branch}",
        ]
    # Not connected — the user creates the repo themselves first.
    return [
        f"Create a new PUBLIC repo named '{repo_name}' at https://github.com/new (no README/gitignore).",
        "git init",
        "git add .",
        'git commit -m "Initial commit — proof of work"',
        f"git branch -M {branch}",
        f"git remote add origin https://github.com/<your-username>/{repo_name}.git",
        f"git push -u origin {branch}",
    ]


def _remember_repo(user_email: str, path_id: str, task_ref: str, repo_url: str, repo_name: str) -> None:
    """Persist the task's repo so the completion post can ground itself in the
    real link later (best-effort — a failed write never fails the repo action)."""
    try:
        _db().learning_progress.update_one(
            {"user_email": user_email, "path_id": path_id},
            {"$set": {f"task_repos.{task_ref}": {
                "repo_url": repo_url, "repo_name": repo_name, "at": _now(),
            }}, "$setOnInsert": {"user_email": user_email, "path_id": path_id, "created_at": _now()}},
            upsert=True,
        )
    except Exception as e:  # pragma: no cover
        logger.warning("github_sync: could not remember repo for %s/%s: %s", user_email, path_id, e)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def ensure_task_repo(user_email: str, path_id: str, task_ref: str) -> Dict:
    """Create/sync the task's GitHub repo (if connected) or return manual steps.

    Returns {repo_url, created, connected, repo_name, default_branch,
             push_instructions:[str], nudges:[str]}. Never pushes the user's files
    automatically; only the empty repo (the consented action) is created."""
    path, task = _load_task(user_email, path_id, task_ref)
    repo_name = _repo_name(path, task_ref)
    description = _repo_description(path, task)
    nudges = push_nudges(task)

    gh = _github_connect()
    connected, login = False, None
    if gh is not None:
        try:
            status = gh.get_status(user_email) or {}
            connected = bool(status.get("connected"))
            login = status.get("login")
        except Exception as e:  # pragma: no cover
            logger.warning("github_sync: get_status failed for %s: %s", user_email, e)
            connected = False

    if not connected:
        return {
            "repo_url": None,
            "created": False,
            "connected": False,
            "repo_name": repo_name,
            "default_branch": "main",
            "push_instructions": _push_instructions(None, "main", connected=False, repo_name=repo_name),
            "nudges": nudges,
            "message": "GitHub isn't connected — create the repo yourself with the steps below, "
                       "or connect GitHub to have us set it up.",
        }

    created, default_branch, repo_url = True, "main", None
    try:
        meta = gh.create_repo(user_email, repo_name, description, private=False)
        repo_url = meta.get("repo_url")
        login = meta.get("login") or login
        default_branch = meta.get("default_branch") or "main"
    except RuntimeError as e:
        if "already exists" in str(e).lower():
            # Sync semantics: the repo is already there — reuse it, don't error.
            created = False
            repo_url = f"https://github.com/{login}/{repo_name}" if login else None
        else:
            logger.warning("github_sync: create_repo failed for %s: %s", user_email, e)
            return {
                "repo_url": None,
                "created": False,
                "connected": True,
                "repo_name": repo_name,
                "default_branch": "main",
                "push_instructions": _push_instructions(None, "main", connected=False, repo_name=repo_name),
                "nudges": nudges,
                "message": f"Couldn't create the repo automatically ({e}); use the manual steps.",
            }

    if repo_url:
        _remember_repo(user_email, path_id, task_ref, repo_url, repo_name)

    return {
        "repo_url": repo_url,
        "created": created,
        "connected": True,
        "repo_name": repo_name,
        "default_branch": default_branch,
        "push_instructions": _push_instructions(repo_url, default_branch, connected=True, repo_name=repo_name),
        "nudges": nudges,
        "message": ("Created your repo — push your work with the steps below."
                    if created else "Repo already exists — push your latest work with the steps below."),
    }


def push_nudges(task: Dict) -> List[str]:
    """Concrete, task-specific push coaching derived in code from the task itself.

    No LLM, no fabrication — every nudge is grounded in this task's proof_type and
    acceptance_criteria so it's specific ("your README must satisfy X"), not generic."""
    task = task or {}
    proof = str(task.get("proof_type") or "").strip().lower()
    ac = [str(c).strip() for c in (task.get("acceptance_criteria") or []) if str(c).strip()]
    blob = " ".join([
        str(task.get("brief") or ""),
        " ".join(ac),
        " ".join(str(c) for c in (task.get("constraints") or [])),
    ]).lower()

    nudges: List[str] = [
        "Commit the failing test first, then the code that makes it pass — the commit "
        "history is your proof you BUILT it, not pasted it.",
        "Add a README whose first line says, to a recruiter, exactly what this proves, "
        "then the run/setup steps.",
    ]
    if proof == "github_pr":
        nudges.append("Open the pull request early as a draft and push commits to it, so the "
                      "diff tells the whole story.")
        nudges.append("Write the PR description as Why / What / Tradeoff — reviewers read that first.")
    if any(k in blob for k in ("latency", "p99", "throughput", "benchmark", "performance", "profil")):
        nudges.append("Add a 'Results' section to the README with the before/after numbers and "
                      "how you measured them.")
    if any(k in blob for k in ("endpoint", " api", "route", "rest", "graphql", "http")):
        nudges.append("Include a copy-pasteable curl example showing the request and the exact "
                      "response — include one error case (e.g. 401 on a missing token).")
    if any(k in blob for k in ("docker", "container", "kubernet", "k8s", "deploy")):
        nudges.append("Commit the Dockerfile/compose and a one-command run so a reviewer goes "
                      "from clone to running in under a minute.")
    if any(k in blob for k in ("test", "coverage", " ci")):
        nudges.append("Wire a GitHub Actions CI badge so the green check is visible from the repo homepage.")
    for c in ac:
        if "readme" in c.lower():
            nudges.append(f"Your README must explicitly satisfy: “{c[:140]}”.")
            break
    if any(k in blob for k in ("tradeoff", "trade-off", "decision", "design choice")):
        nudges.append("Add a short 'Design decisions' note explaining the tradeoff you made on X "
                      "and what you rejected.")

    seen, out = set(), []
    for n in nudges:
        if n not in seen:
            seen.add(n)
            out.append(n)
        if len(out) >= 6:
            break
    return out

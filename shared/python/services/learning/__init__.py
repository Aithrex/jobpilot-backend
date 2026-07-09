"""`services.learning` — the Learning / Growth loop as a bounded context (ADR-003).

This package UNIFIES the résumé→hired→noticed loop under one namespace without
moving or breaking the existing flat service modules. The already-shipped surface
(§6.6 Suggest / Plan / Verify + the classic Learning Hub) is RE-EXPORTED from its
current homes; the NEW get-noticed pieces (GitHub push-sync, verified-only
completion posts) live as submodules here.

    from services.learning import (
        build_differentiators_job, build_differentiation_plan_job,   # suggest + plan
        build_verify_job, verify_task,                               # verify (proof-gated)
        build_plan_job, complete_module, list_paths, get_path,       # learning hub
        ensure_task_repo, push_nudges,                               # github_sync (NEW)
        draft_completion_post, publish_completion_post,              # publish (NEW)
    )

Import-safe: nothing here touches Mongo/Bedrock/OAuth at import time — all
side-effects happen inside functions, lazily (the flat modules it re-exports are
already import-safe under the provider abstraction).
"""
from __future__ import annotations

# --- existing surface, re-exported (do NOT rewrite the flat modules) ---------
from services.differentiation_service import (  # noqa: F401
    build_differentiators_job,
    build_differentiation_plan_job,
)
from services.learning_verifier import (  # noqa: F401
    build_verify_job,
    verify_task,
)
from services.learning_service import (  # noqa: F401
    build_plan_job,
    complete_module,
    list_paths,
    get_path,
)

# --- new submodules (this package) -------------------------------------------
from services.learning import github_sync, publish  # noqa: F401
from services.learning.github_sync import (  # noqa: F401
    ensure_task_repo,
    push_nudges,
)
from services.learning.publish import (  # noqa: F401
    draft_completion_post,
    publish_completion_post,
    cancel_scheduled_post,
    send_scheduled_post,
    is_task_verified,
)

__all__ = [
    # suggest + plan
    "build_differentiators_job",
    "build_differentiation_plan_job",
    # verify (proof-gated)
    "build_verify_job",
    "verify_task",
    # learning hub
    "build_plan_job",
    "complete_module",
    "list_paths",
    "get_path",
    # github push-sync (NEW)
    "github_sync",
    "ensure_task_repo",
    "push_nudges",
    # publish / get-noticed (NEW)
    "publish",
    "draft_completion_post",
    "publish_completion_post",
    "cancel_scheduled_post",
    "send_scheduled_post",
    "is_task_verified",
]

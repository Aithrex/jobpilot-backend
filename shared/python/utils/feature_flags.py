"""Feature flags for the resume tailoring pipeline — one registry, one default.

Why this module exists: flags used to be read with a helper that defaulted to
ON when ``ENVIRONMENT == "development"`` and OFF everywhere else. That meant
local runs, the test suite and the eval harness all exercised a pipeline that
production never ran, so a green suite said nothing about what users received.

Every flag here has ONE default that applies in every environment. Defaults
are pinned to the values production was already running, so introducing this
registry changed no generated content; flipping a flag is now a deliberate,
reviewable act rather than a side effect of where the code happens to run.

Override with the environment variable of the same name:

    SECTION_REFINE_SUMMARY=true

Truthy values: 1, true, yes, on (case-insensitive). Anything else is false.

Read flags at call time via ``enabled()`` when the value must be live (routes,
services). The module-level constants some services expose are snapshots taken
at import; tests monkeypatch those constants directly.
"""
from __future__ import annotations

import os
from typing import Dict

_TRUTHY = ("1", "true", "yes", "on")

# name -> default. Keep this list alphabetical and annotated: it is the only
# place a reader can see what the pipeline's shape is by default.
DEFAULTS: Dict[str, bool] = {
    # Opt-in two-page rendering for senior resumes that genuinely overflow one
    # page at the minimum body size.
    "ALLOW_TWO_PAGE": True,
    # Full-JD requirement records + head/tail LLM window. Additive sidecar;
    # flat skill lists stay. On so a license in the last paragraph is seen.
    "JD_ANALYZER_V2": True,
    # Deterministic plan-guided writers (evidence ids, skill gate, section
    # plan). Does not invent bullets. On so every published claim is sourced.
    "PLAN_GUIDED_WRITERS": True,
    # When on, writers replace ContentAugmenter instead of shadowing it.
    # Off by default so software resumes keep the augmenter; licensed
    # professions skip the augmenter regardless (see finalize()).
    "PLAN_WRITERS_REPLACE_AUGMENTER": False,
    # After the integrity-clean draft, a FLASH judge scores quality and ONE
    # regeneration is attempted below bar. Fail-open; costs an extra call.
    "QUALITY_REFLECTION_GATE": False,
    # Post-integrity LLM polish of the summary against the settled bullets.
    # Discards the rewrite if it introduces an ungrounded claim.
    "SECTION_REFINE_SUMMARY": False,
    # FLASH pass that files a new JD term under one of the candidate's own
    # skill headings instead of the deterministic taxonomy bucket.
    "SKILLS_CATEGORIZER": False,
    # Preserve the base resume's skill category order and within-category
    # order, appending JD additions rather than reordering for density.
    "SKILLS_PRESERVE_LAYOUT": False,
    # Injects a cross-user "these keywords correlate with higher ATS" strategy
    # hint into the tailor prompt. Never injects content. Fails open.
    "USE_ATS_MARKET_SIGNALS": False,
}


def enabled(name: str) -> bool:
    """Return whether ``name`` is on, using DEFAULTS when unset.

    Unregistered names default to False rather than raising: a stale flag read
    in an old code path should degrade to "feature off", not crash a job.
    """
    raw = os.environ.get(name)
    if raw is None:
        return DEFAULTS.get(name, False)
    return raw.strip().lower() in _TRUTHY

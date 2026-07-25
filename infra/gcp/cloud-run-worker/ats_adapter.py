"""ATS adapter seam (P4/P5 handoff).

The worker does NOT reimplement per-ATS form logic. It calls this documented
interface; the REAL deterministic adapters live in
``shared/python/services/ats_adapters.py`` (Greenhouse / Lever / Ashby / Workday /
Generic) and are driven by ``shared/python/services/fill_driver.py`` — this seam
bridges those onto the worker's coarse ``open / fill / attach_resume /
click_submit / classify`` calls. The form logic has exactly ONE home; nothing is
duplicated here.

An adapter (the worker's view) implements — all against a live Playwright
``page``:

    class AtsAdapter:
        family: str
        def open(self, page, apply_url) -> None
            Navigate + wait for the form to be interactive.
        def fill(self, page, answers: dict) -> tuple[int, list]
            Fill the vetted {field_id: value} map (recon → option-match → fill,
            multi-step advance to Review). MUST NOT invent a value for a field
            absent from ``answers``. Returns (filled_count, skipped_ids). Does NOT
            submit (the worker's /precheck gate runs BETWEEN fill and submit).
        def attach_resume(self, page, resume_path: str) -> None
            Attach the downloaded résumé PDF.
        def click_submit(self, page) -> None
            Click the ATS submit button once (never retries).
        def classify(self, page) -> str
            Post-submit page → "submitted" | "waiting_verify" | "waiting_human" |
            "submission_unknown" | "failed". Never blocks holding the browser.
        def receipt(self) -> dict | None
            The structured, PII-FREE receipt of the attempt (P1-SHOT: preferred
            over a screenshot).

Resolution order for ``get_adapter``:
  1. the shared ``services.ats_adapters`` + ``services.fill_driver`` (bundled in
     the image / on the layer) — the real P4 adapters, bridged below;
  2. an installed ``jobpilot_ats_adapters`` package exposing ``get_adapter``;
  3. None → the worker reports a clean ``failed`` (never a fabricated submit).
"""
from __future__ import annotations

import importlib
import logging
from typing import Optional

logger = logging.getLogger("worker.ats")

_FORM_TIMEOUT_MS = 15_000
_NAV_TIMEOUT_MS = 30_000

# Résumé/attachment file-input selectors, in preference order (mirrors the mature
# BrowserExecutor). The résumé is executor-supplied, never a question the driver
# fills.
_RESUME_SELECTORS = (
    "#resume", "#s3_upload_for_resume", "#_systemfield_resume",
    "input[type=file][name=resume]",
    "input[type=file][data-automation-id=resume]", "input[type=file]",
)


class _SharedAtsAdapter:
    """Bridge: the worker's coarse interface over the shared deterministic
    adapter + the fill driver (recon → normalize → resolve/option-match → fill →
    multi-step advance → submit → classify). No ATS logic lives here."""

    def __init__(self, ats_adapters_mod, fill_driver_mod, family: str):
        self._ats = ats_adapters_mod
        self._fd = fill_driver_mod
        self.family = family
        self._adapter = None
        self._submit_result = None

    def open(self, page, apply_url: str) -> None:
        page.goto(apply_url, wait_until="domcontentloaded", timeout=_NAV_TIMEOUT_MS)
        self._adapter = self._ats.select_adapter(page, apply_url)

    def fill(self, page, answers: dict):
        # Prepare-only: fills every vetted field + advances multi-step to Review,
        # but NEVER submits (the worker's /precheck gate runs before submit).
        outcome = self._fd.drive(self._adapter, answers or {},
                                 mode=self._fd.MODE_COPILOT)
        self._fill_outcome = outcome
        snap = outcome.get("review_snapshot") or {}
        return len(snap.get("fields") or []), []

    def attach_resume(self, page, resume_path: str) -> None:
        payload = {"name": "resume.pdf", "mimeType": "application/pdf"}
        for sel in _RESUME_SELECTORS:
            try:
                if page.query_selector(sel) is not None:
                    page.set_input_files(sel, files=[dict(payload, buffer=_read(resume_path))])
                    return
            except Exception:
                continue

    def click_submit(self, page) -> None:
        # The shared adapter's submit() clicks ONCE and classifies (takeover /
        # confirmation / ambiguity). We stash the structured result for classify.
        self._submit_result = self._adapter.submit()

    def classify(self, page) -> str:
        st = (self._submit_result or {}).get("status")
        if st == self._ats.ADAPTER_SUBMITTED:
            return "submitted"
        if st == self._ats.ADAPTER_SUBMISSION_UNKNOWN:
            return "submission_unknown"
        if st == self._ats.ADAPTER_WAITING_HUMAN:
            return "waiting_human"
        return "failed"

    def receipt(self):
        return (self._submit_result or {}).get("receipt")


def _read(path: str) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


def get_adapter(ats_family: Optional[str]):
    """Resolve the ATS adapter for a family, or None if none is available."""
    family = (ats_family or "").strip().lower()

    # 1) The real shared adapters + fill driver (bundled on the layer / image).
    try:
        ats_adapters_mod = importlib.import_module("services.ats_adapters")
        fill_driver_mod = importlib.import_module("services.fill_driver")
        return _SharedAtsAdapter(ats_adapters_mod, fill_driver_mod, family)
    except Exception as e:
        logger.info("shared ats_adapters not importable (%s); trying package", e)

    # 2) A vendored/pip-installed adapter package.
    try:
        mod = importlib.import_module("jobpilot_ats_adapters")
    except ModuleNotFoundError:
        logger.info("no ATS adapter package installed (family=%s)", family)
        return None
    try:
        return mod.get_adapter(family)
    except Exception as e:  # pragma: no cover - adapter package's own errors
        logger.warning("ATS adapter resolve failed for %s: %s", family, e)
        return None

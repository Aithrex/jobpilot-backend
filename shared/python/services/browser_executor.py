"""BrowserExecutor — the real Playwright/Chromium ``ExecutorAdapter`` that drives
an ATS application form end-to-end and clicks submit.

This is the piece the HTTP recon executor cannot do: actually POST an
application. Greenhouse/Lever gate their submission API behind the *employer's*
key (which a third party never has), so the only route is driving the public
candidate web form as a human would — a real browser. This class does that.

Design / contract (see ``executor_adapter`` for the full spec):
  * ``recon_form`` is delegated to the cheap HTTP path (``AtsExecutor``) for
    Greenhouse; the browser is reserved for what genuinely needs it (submit).
  * ``submit`` launches headless Chromium, fills the form from the vetted
    ``answers`` map (``{field_id: value}`` where ``field_id`` is the CANONICAL
    question-bank id — ``full_name``/``email``, NOT the DOM handle: the drive
    re-recons the live form and maps each canonical id onto the element recon
    actually found), attaches the résumé PDF, clicks submit, and classifies the
    outcome:
        SUBMIT_SUCCESS        confirmation detected  (+ screenshot_ref)
        SUBMIT_WAITING_VERIFY a CAPTCHA / email-verify wall  (freeze/thaw)
        SUBMIT_FAILED         hard validation/other failure
        EXECUTOR_UNAVAILABLE  TRANSIENT: could not act now → retry later
        EXECUTOR_UNSUPPORTED  PERMANENT: this ATS is not one we can drive →
                              ledger `unsupported` + `user_message` (spec §4)
        EXECUTOR_PREPARED     dry-run/copilot: filled + proof, submit withheld —
                              NOT a failure (mirrors fill_driver.FILL_PREPARED)
    It NEVER invents a value for a field not in ``answers`` — identity,
    employment history and every legal attestation come from the caller's
    resolved answers only (AUTOAPPLY_REPAIR_SPEC §0) — and NEVER raises for an
    expected condition: every path returns a structured dict.
  * The résumé is resolved to bytes via an injectable ``resume_resolver`` (the
    default resolves ``tailoring_record_id`` — else the base ``resume_asset_id``
    — → S3, always owner-scoped; tests inject bytes directly),
    and submission-proof screenshots go through an injectable ``screenshot_sink``
    (default → S3), so the whole thing is testable with zero AWS.

Runtime: this class runs INSIDE the container-image Lambda (``jobpilot-apply-
executor``); the resume Lambda reaches it through ``LambdaInvokeExecutor``. It is
also directly drivable in-process for local validation (that's how its e2e test
proves the fill→attach→submit→confirm→screenshot loop against a form fixture).
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any, Callable, Dict, List, Optional, Tuple

from services.ats_detector import detect_ats, GREENHOUSE, LEVER, ASHBY, WORKDAY
from services.ats_executor import AtsExecutor
from services.executor_adapter import (
    ExecutorAdapter,
    EXECUTOR_UNAVAILABLE,
    ORACLE_READY,
    SUBMIT_SUCCESS,
    SUBMIT_WAITING_VERIFY,
    SUBMIT_FAILED,
    WALL_CAPTCHA,
    WALL_VERIFY,
    QTYPE_TEXT,
    QTYPE_TEXTAREA,
    QTYPE_SELECT,
    QTYPE_CHECKBOX,
    QTYPE_RADIO,
    QTYPE_FILE,
)

# Lever field type (native) → executor QTYPE_* vocabulary.
_LEVER_TYPE_MAP = {
    "text": QTYPE_TEXT, "email": QTYPE_TEXT, "tel": QTYPE_TEXT, "url": QTYPE_TEXT,
    "textarea": QTYPE_TEXTAREA, "select": QTYPE_SELECT,
    "checkbox": QTYPE_CHECKBOX, "radio": QTYPE_RADIO, "file": QTYPE_FILE,
}

# Browser-side extraction of an application form (Lever / Ashby): one entry per
# field (handle = id OR name — Lever keys by name, Ashby by id), collapsing
# radio/checkbox groups, with best-effort label + required.
_FORM_RECON_JS = r"""
() => {
  const out = [], seen = new Set();
  const labelFor = (el) => {
    if (el.labels && el.labels.length) return (el.labels[0].innerText||'').trim();
    if (el.getAttribute('aria-label')) return el.getAttribute('aria-label').trim();
    const q = el.closest('.application-question, .application-field, li, .field, div, fieldset');
    if (q) { const l = q.querySelector('label, .application-label'); if (l) return (l.innerText||'').trim(); }
    return el.getAttribute('placeholder') || '';
  };
  // Choice labels for an ENUMERATED control. Without these the needs_input item
  // reaches the FE as a select with no options, the FE falls back to a free-text
  // box, and nothing the user types can ever match a real option — the row
  // re-parks on the same question forever. <select> reads its own <option>s;
  // a radio/checkbox GROUP (which we collapse by handle below) reads its
  // same-named siblings' labels.
  const optionsFor = (el) => {
    const clean = (s) => (s || '').replace(/\s+/g, ' ').trim().slice(0, 200);
    let vals = [];
    if (el.tagName === 'SELECT') {
      vals = [...el.options]
        .filter(o => !o.disabled && (o.value !== '' || clean(o.text)))
        .map(o => clean(o.text) || clean(o.value));
    } else if (['radio', 'checkbox'].includes((el.type || '').toLowerCase()) && el.name) {
      const scope = el.form || document;
      vals = [...scope.querySelectorAll(
        'input[type=' + el.type.toLowerCase() + ']')]
        .filter(s => s.name === el.name)
        .map(s => clean(labelFor(s)) || clean(s.value));
    }
    const outv = [], got = new Set();
    for (const v of vals) {
      // Drop the placeholder row ("Select...", "-- Please choose one --") and
      // dupes. Anchored at BOTH ends and \b-bounded so a real choice that
      // merely starts with the word ("Selected candidates only") survives —
      // silently deleting a valid option is worse than keeping a placeholder,
      // which the fill driver would never match anyway.
      if (!v || got.has(v)) continue;
      if (/^[-\s]*(please\s+)?(select|choose)\b[\s\w.…]{0,24}[-\s.…]*$/i.test(v)) continue;
      got.add(v); outv.push(v);
      if (outv.length >= 40) break;
    }
    return outv;
  };
  document.querySelectorAll('input, select, textarea').forEach(el => {
    const type = el.tagName === 'SELECT' ? 'select'
               : el.tagName === 'TEXTAREA' ? 'textarea'
               : (el.type || 'text').toLowerCase();
    if (['hidden','submit','button','reset'].includes(type)) return;
    const handle = el.id || el.name || '';
    if (!handle || seen.has(handle)) return;   // collapse radio/checkbox groups
    seen.add(handle);
    const entry = { name: handle, type, label: labelFor(el) || handle,
                    required: el.required || el.getAttribute('aria-required') === 'true' };
    const opts = optionsFor(el);
    if (opts.length) entry.options = opts;
    out.push(entry);
  });
  return out;
}
"""

logger = logging.getLogger(__name__)

_IN_LAMBDA = bool(os.getenv("AWS_LAMBDA_FUNCTION_NAME"))


def chromium_launch_args():
    """Chromium flags. Base set works everywhere; the Lambda extras
    (`--single-process`/`--no-zygote`) are REQUIRED under the Lambda sandbox —
    without them Chromium's multi-process/zygote model can't fork and the
    browser dies immediately on launch ("Target ... has been closed")."""
    args = ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
    if _IN_LAMBDA:
        args += ["--single-process", "--no-zygote",
                 "--disable-setuid-sandbox", "--disable-software-rasterizer"]
    return args


def browser_context_kwargs() -> Dict[str, Any]:
    """Shared Playwright context options — Lambda defaults to a tiny viewport
    that clips Workday Create Account / Sign In submit buttons below the fold."""
    return {
        "viewport": {"width": 1440, "height": 1100},
        "user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
    }


_NAV_TIMEOUT_MS = 30_000
_FORM_TIMEOUT_MS = 15_000
_SUBMIT_SETTLE_MS = 6_000

# Confirmation / wall / error signals (case-insensitive text probes).
_CONFIRM_RE = re.compile(
    r"(thank you for applying|application (has been )?submitted|"
    r"we[’'`]?ve received your application|your application (has been|was) "
    r"(sent|submitted|received)|successfully submitted)", re.IGNORECASE)
_CONFIRM_SELECTORS = (
    "#application-confirmation", "#application_confirmation",
    "[class*=confirmation]", "[data-testid*=confirmation]",
)
# Only the CHALLENGE surface counts as a wall — the reCAPTCHA `bframe` (image
# grid) or the hCaptcha challenge. The passive Enterprise/v3 badge and the
# invisible anchor ride silently on most Greenhouse forms and pass on score;
# matching them would falsely freeze every submission.
_CAPTCHA_SELECTORS = (
    "iframe[src*=bframe]",                 # reCAPTCHA v2/Enterprise challenge modal
    "iframe[src*='hcaptcha.com/captcha']",  # hCaptcha challenge
    "iframe[title*='recaptcha challenge' i]",
)
_VERIFY_RE = re.compile(
    r"(verify your email|confirm your email|check your (inbox|email)|"
    r"a verification (link|code) |security code|enter the code)", re.IGNORECASE)
# A react-select combobox on Greenhouse: input carrying this class / role.
_SELECT_HINT = "select__input"

# The page/context/browser died under the drive (Playwright wording). TRANSIENT
# infrastructure fault — Chromium in the Lambda sandbox runs --single-process, so
# a crash/OOM takes the target with it. Nothing was submitted when this fires.
_CLOSED_RE = re.compile(
    r"(target (page, context or browser )?(has been )?closed|"
    r"browser has been closed|context has been closed|"
    r"target closed|session closed|page has been closed|"
    r"connection closed|browser closed)", re.IGNORECASE)

# Which file input is the résumé — matched against the input's OWN attributes
# (id/name/aria-label/accept/class), never a per-vendor id list.
_RESUME_RE = re.compile(r"resum|cv\b|curriculum", re.IGNORECASE)
_COVER_LETTER_RE = re.compile(r"cover[\s_-]*letter", re.IGNORECASE)

# The ATS families this executor can actually drive end-to-end. Anything else is
# PERMANENTLY unsupported for auto-apply (iCIMS / SmartRecruiters / Workable are
# merely *detected*; an unrecognised host is not automatable at all).
_SUPPORTED_FAMILIES = frozenset({GREENHOUSE, LEVER, ASHBY, WORKDAY})

# Result status for "this employer's system is one we cannot drive". PERMANENT
# and user-facing — strictly distinct from EXECUTOR_UNAVAILABLE, which means
# "the fleet could not act right now, retry later". The orchestrator maps this
# onto the ledger's STATUS_UNSUPPORTED (AUTOAPPLY_REPAIR_SPEC §4); it must never
# be surfaced as an opaque technical failure.
EXECUTOR_UNSUPPORTED = "executor_unsupported"
UNSUPPORTED_USER_MESSAGE = (
    "We can't auto-apply to this employer's system yet — please apply manually."
)

# Dry-run / copilot outcome: the form was filled and proof captured, and Submit
# was DELIBERATELY never clicked. Mirrors ``fill_driver.FILL_PREPARED`` (kept as
# a literal so this module stays import-light). ``submitted`` is always False —
# this is NOT a failure and must never mark the ledger row failed.
EXECUTOR_PREPARED = "prepared"


# ---------------------------------------------------------------------------
# Injectable defaults (S3-backed) — kept import-light so tests need no AWS.
# ---------------------------------------------------------------------------
def _safe_upload_name(filename: Any, mime_type: Any = "") -> str:
    """A safe file name for the browser upload that KEEPS the real extension —
    a .docx attached as "resume.pdf" is rejected by the ATS. Basename only, no
    path separators, no exotic characters."""
    base = os.path.basename(str(filename or "")).strip()
    base = re.sub(r"[^A-Za-z0-9._-]", "_", base)[:80].lstrip(".")
    if base and "." in base:
        return base
    mt = str(mime_type or "").lower()
    return "resume.docx" if ("word" in mt or "officedocument" in mt) else "resume.pdf"


def _default_resume_resolver(application: Dict[str, Any],
                             resume_pdf: Any,
                             user_email: Optional[str]) -> Optional[Tuple[str, bytes]]:
    """Resolve a résumé REFERENCE → (filename, file_bytes) from S3.

    Two reference shapes (see ``auto_apply_service._resume_ref``):
      * ``{tailoring_record_id}`` — the pinned tailored variant (wins);
      * ``{resume_asset_id}``     — the user's active BASE upload, used when
        tailoring is off/skipped for the row.
    BOTH lookups are owner-scoped: without a ``user_email`` the base id does not
    resolve at all, so an id alone can never reach another user's résumé.

    Returns None when no résumé is linked; the caller then hands the row to
    ``_presubmit_gaps``, which PARKS it (``resume_missing``) rather than
    submitting a CV-less application. Never raises past the caller's try/except.
    """
    ref = resume_pdf if isinstance(resume_pdf, dict) else {}
    trid = ref.get("tailoring_record_id")
    asset_id = ref.get("resume_asset_id")
    if not trid and not asset_id:
        return None
    from utils.db_connect import DBConnect
    from services.s3_service import get_storage_service

    if not trid:
        # BASE résumé fallback — owner-scoped by construction (no user_email ⇒
        # no résumé, never a cross-user read).
        from services import resume_access
        asset = resume_access.get_owned_asset(user_email or "", asset_id)
        object_key = (asset or {}).get("object_key")
        if not object_key:
            return None
        return (_safe_upload_name(asset.get("filename"), asset.get("mime_type")),
                get_storage_service().get_resume(object_key))

    db = DBConnect().get_db()
    q: Dict[str, Any] = {"record_id": trid}
    if user_email:
        q["user_email"] = user_email
    rec = db.tailoring_records.find_one(q)
    if not rec:
        return None
    # Prefer a cached rendered PDF on the current/only version.
    versions = rec.get("versions") or []
    version = next((v for v in versions
                    if v.get("version_id") == rec.get("current_version_id")), None)
    version = version or (versions[-1] if versions else None)
    s3_key = None
    if version:
        s3_key = ((version.get("files") or {}).get("pdf") or {}).get("s3_key")
    storage = get_storage_service()
    if s3_key:
        return ("resume.pdf", storage.get_resume(s3_key))
    # Cache miss → render on the fly, then it's still just bytes to attach.
    from services.resume_service import get_resume_service
    tailored = rec.get("tailored_resume") or {}
    pdf = get_resume_service().renderer.generate_pdf(tailored)
    return ("resume.pdf", pdf)


def _default_screenshot_sink(application: Dict[str, Any], png: bytes) -> Optional[str]:
    """Upload submission proof to S3 under an audit prefix; return the key."""
    try:
        import time
        from services.s3_service import get_storage_service
        storage = get_storage_service()
        aid = application.get("application_id") or "unknown"
        key = f"auto-apply-audit/{aid}/{int(time.time())}.png"
        put_kwargs = {
            "Bucket": storage.bucket,
            "Key": key,
            "Body": png,
            "ContentType": "image/png",
            # Bucket default is aws:kms; declare explicitly so IAM ViaService
            # GenerateDataKey is exercised and proofs stay SSE-KMS.
            "ServerSideEncryption": "aws:kms",
        }
        cmk = (os.getenv("APPLY_EXECUTOR_KMS_KEY_ID") or "").strip()
        if cmk:
            put_kwargs["SSEKMSKeyId"] = cmk
        storage.client.put_object(**put_kwargs)
        return key
    except Exception as e:  # pragma: no cover - audit is best-effort
        logger.warning("screenshot upload failed: %s", e)
        return None


# ---------------------------------------------------------------------------
# Workday answer normalization — NO fabrication (AUTOAPPLY_REPAIR_SPEC §0)
# ---------------------------------------------------------------------------
# Key ALIASES only: the caller may address the same field by either name. This
# is normalization of a key, never the invention of a value.
_WD_ALIASES = {
    "address_line1": "address_line_1",
    "current_title": "job_title",
    "current_company": "company",
    "formfield_country": "country",
    "formfield_datesignedon": "date_signed",
    "formfield_disabilityform": "disability_status",
}

# Canonical decline token — kept identical to
# ``application_question_bank.DECLINE`` (asserted by the tests) without importing
# that module here. ``fill_driver`` option-matches it onto whatever decline
# wording THIS form uses ("I don't wish to answer", "Decline to State (United
# States of America)", "Prefer not to answer", …).
_DECLINE = "Decline to self-identify"

# Protected-EEO fields (gender / ethnicity / veteran / disability). Declining is
# the ONE auto-default allowed without a declared value: it asserts NOTHING about
# the user (spec §0 rule 3). Note "Hispanic or Latino? -> No" is an assertion
# about ethnicity, so it declines too — it is never answered "No" for the user.
_WD_EEO_DECLINES = {
    "gender": _DECLINE,
    "hispanic_latino": _DECLINE,
    "race_ethnicity": _DECLINE,
    "veteran_status": _DECLINE,
    "disability_status": _DECLINE,
    # Workday's CC-305 section is reconned by its raw automation id when the
    # section label does not normalize to a catalog field.
    "formfield_disabilityform": _DECLINE,
    "formField-disabilityForm": _DECLINE,
}

# Non-assertive form mechanics: they describe HOW we fill the form, not a fact
# about the user, and are safe to default.
_WD_SAFE_DEFAULTS = {
    "phone_device_type": "Mobile",
    "language": "English",
}

# The signature date on a self-identification form is genuinely "today".
_WD_DATE_SIGNED_FIELDS = ("date_signed", "formfield_datesignedon")


def _workday_answers(answers: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Return the answer map the Workday wizard is driven from.

    HARD RULE (AUTOAPPLY_REPAIR_SPEC §0 rules 1/2/4): this NEVER invents identity
    (name, email, phone, address), employment facts (employers, titles, dates,
    role descriptions) or a legal attestation (work authorization, sponsorship,
    citizenship, export-control person status, age, prior/related employment,
    terms acceptance). Those may come ONLY from the caller's resolved ``answers``
    — the user's declared preferences + their résumé. A value we do not have
    stays ABSENT, so ``fill_driver`` parks the run at ``needs_input`` /
    ``waiting_human`` naming the exact field instead of asserting something false
    to a real employer.

    The only additions here are the key aliases above, the two non-assertive
    mechanics, today's signature date, and the protected-EEO declines. There is
    deliberately NO blanket default map — one used to live here and it filled a
    hardcoded person's name plus inverted work-authorization / sponsorship /
    citizenship answers into every Workday application.
    """
    out: Dict[str, Any] = dict(answers or {})
    for canon, alias in _WD_ALIASES.items():
        if canon not in out and alias in out:
            out[canon] = out[alias]
    for key, value in _WD_SAFE_DEFAULTS.items():
        if out.get(key) in (None, ""):
            out[key] = value
    for key, value in _WD_EEO_DECLINES.items():
        if out.get(key) in (None, ""):
            out[key] = value
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).strftime("%m/%d/%Y")
    for key in _WD_DATE_SIGNED_FIELDS:
        if out.get(key) in (None, ""):
            out[key] = today
    return out


class BrowserExecutor(ExecutorAdapter):
    """Playwright-driven submit executor. See module docstring."""

    name = "playwright-chromium"

    def __init__(self, *,
                 resume_resolver: Optional[Callable] = None,
                 screenshot_sink: Optional[Callable] = None,
                 user_email: Optional[str] = None,
                 headless: bool = True,
                 enabled: bool = True):
        self._recon = AtsExecutor()
        self._resolve_resume = resume_resolver or _default_resume_resolver
        self._save_screenshot = screenshot_sink or _default_screenshot_sink
        self._user_email = user_email
        self._headless = headless
        self._enabled = enabled

    # -- liveness ------------------------------------------------------------
    def is_available(self) -> bool:
        return bool(self._enabled)

    # -- recon --------------------------------------------------------------
    def recon_form(self, apply_url: str, ats_family: Optional[str]) -> Dict[str, Any]:
        """Greenhouse recon is cheap HTTP (delegate to AtsExecutor). Lever/Ashby/
        Workday expose no public question API, so recon their form in the browser."""
        family, ids = detect_ats(apply_url, ats_family)
        if family == LEVER:
            return self._recon_browser(self._lever_apply_url(ids, apply_url),
                                       family, {"resume", "cover_letter"}, ("eeo",))
        if family == ASHBY:
            return self._recon_browser(self._ashby_apply_url(ids, apply_url),
                                       family, {"_systemfield_resume"}, ())
        if family == WORKDAY:
            return self._recon_workday(apply_url)
        if family not in _SUPPORTED_FAMILIES:
            # A vendor we cannot drive (iCIMS / SmartRecruiters / Workable are
            # detected but unimplemented; an unknown host is not automatable at
            # all). PERMANENT → the row should land ledger STATUS_UNSUPPORTED.
            # NOTE (contract): ``status`` deliberately stays EXECUTOR_UNAVAILABLE
            # so an orchestrator that has not yet learned the new vocabulary
            # keeps its SAFE behaviour (leave the row queued) instead of treating
            # an empty question list as a real, fully-answered form. The
            # ``unsupported`` flag (+ ``unsupported_status``) is what the §4
            # wiring keys off to mark the row unsupported.
            logger.info("browser recon: family=%s not supported — unsupported vendor",
                        family)
            fam = (family or "").strip().lower() or "unknown"
            return {"questions": [], "needs_account": False,
                    "status": EXECUTOR_UNAVAILABLE,
                    "unsupported": True,
                    "unsupported_status": EXECUTOR_UNSUPPORTED,
                    "ats_family": fam, "reason": f"unsupported_ats:{fam}",
                    "user_message": UNSUPPORTED_USER_MESSAGE,
                    "retryable": False}
        return self._recon.recon_form(apply_url, ats_family)

    def _recon_workday(self, apply_url: str) -> Dict[str, Any]:
        """Open a Workday job → Apply CTA → adapter.recon() on the first step.

        Account/OTP gates are reported via ``needs_account`` so gather can land
        ``needs_input`` instead of silently leaving the row ``queued`` forever
        (AtsExecutor has no Workday question API).
        """
        from playwright.sync_api import sync_playwright
        from services import ssrf_guard
        from services.ats_adapters import select_adapter, WorkdayAdapter

        allowed, reason = ssrf_guard.apply_url_allowed(apply_url or "")
        if not allowed:
            logger.info("workday recon ssrf_blocked:%s url=%s", reason, apply_url)
            return {"questions": [], "needs_account": False, "status": EXECUTOR_UNAVAILABLE}

        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=self._headless,
                                             args=chromium_launch_args())
                try:
                    context = browser.new_context(**browser_context_kwargs())
                    ssrf_guard.install_route_guard(context)
                    page = context.new_page()
                    page.set_default_timeout(_FORM_TIMEOUT_MS)
                    page.goto(apply_url, wait_until="domcontentloaded",
                              timeout=_NAV_TIMEOUT_MS)
                    try:
                        page.wait_for_load_state("networkidle", timeout=12_000)
                    except Exception:
                        page.wait_for_timeout(2_500)

                    adapter = select_adapter(page, apply_url) or WorkdayAdapter(page, apply_url)
                    if hasattr(adapter, "click_apply_cta"):
                        try:
                            if adapter.click_apply_cta():
                                page.wait_for_timeout(2_500)
                                try:
                                    page.wait_for_load_state("networkidle",
                                                             timeout=10_000)
                                except Exception:
                                    pass
                                adapter = select_adapter(page, page.url) or adapter
                        except Exception as e:
                            logger.info("workday recon apply CTA: %s", e)

                    needs_account = False
                    try:
                        needs_account = bool(
                            getattr(adapter, "_account_gate_present", lambda: False)()
                            or getattr(adapter, "_otp_gate_present", lambda: False)())
                    except Exception:
                        pass

                    raw_qs = []
                    try:
                        raw_qs = adapter.recon() if adapter else []
                    except Exception as e:
                        logger.info("workday recon partial: %s", e)

                    # Map adapter shape → gather's normalize_recon_questions input.
                    questions = []
                    for q in raw_qs or []:
                        if not isinstance(q, dict):
                            continue
                        qid = (q.get("recon_id") or q.get("id") or q.get("field_id")
                               or q.get("handle") or "")
                        label = (q.get("label") or q.get("text") or qid or "").strip()
                        if not qid and not label:
                            continue
                        questions.append({
                            "id": str(qid or label)[:200],
                            "text": label[:300],
                            "type": q.get("type") or "text",
                            "required": bool(q.get("required")),
                            "is_legal_attestation": bool(q.get("is_legal_attestation")),
                            "options": q.get("options") or [],
                        })
                        if len(questions) >= 120:
                            break

                    # Empty questions + account gate still counts as a successful
                    # recon (gather can pause for credentials). Only hard-fail when
                    # we got neither fields nor a recognizable gate.
                    if not questions and not needs_account:
                        logger.info("workday recon empty url=%s", apply_url)
                        return {"questions": [], "needs_account": False,
                                "status": EXECUTOR_UNAVAILABLE}
                    return {"questions": questions, "needs_account": needs_account}
                finally:
                    browser.close()
        except Exception as e:
            logger.warning("workday recon failed (%s): %s", apply_url, e)
            return {"questions": [], "needs_account": False, "status": EXECUTOR_UNAVAILABLE}

    def _recon_browser(self, target: str, family: str, exclude_names: set,
                       legal_prefixes: tuple) -> Dict[str, Any]:
        """Generic browser recon for name/id-keyed forms (Lever, Ashby)."""
        from playwright.sync_api import sync_playwright
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=self._headless,
                                             args=chromium_launch_args())
                try:
                    page = browser.new_page()
                    page.set_default_timeout(_FORM_TIMEOUT_MS)
                    page.goto(target, wait_until="domcontentloaded", timeout=_NAV_TIMEOUT_MS)
                    self._wait_for_form(page)
                    raw = page.evaluate(_FORM_RECON_JS)
                finally:
                    browser.close()
        except Exception as e:
            logger.warning("%s recon failed (%s): %s", family, target, e)
            return {"questions": [], "needs_account": False, "status": EXECUTOR_UNAVAILABLE}

        return {"questions": self._shape_recon_fields(raw, exclude_names,
                                                      legal_prefixes),
                "needs_account": False}

    @staticmethod
    def _shape_recon_fields(raw: Optional[List[Dict[str, Any]]],
                            exclude_names: set,
                            legal_prefixes: tuple) -> List[Dict[str, Any]]:
        """Browser-captured fields → ReconQuestion dicts. Split out of
        ``_recon_browser`` so the shaping (which is where option labels used to
        be dropped) is testable without a browser."""
        from services.ats_executor import _is_legal
        questions, seen = [], set()
        for f in raw or []:
            name = (f.get("name") or "").strip()
            if not name or name in seen:
                continue
            # Executor-supplied résumé/cover-letter file fields → drop.
            if (name in exclude_names or f.get("type") == "file"
                    or name.startswith(("resume", "cover_letter"))):
                continue
            seen.add(name)
            label = (f.get("label") or name).strip()
            qtype = _LEVER_TYPE_MAP.get(f.get("type"), QTYPE_TEXT)
            is_legal = bool(_is_legal(label) or any(name.startswith(p) for p in legal_prefixes))
            q: Dict[str, Any] = {
                "id": name, "text": label, "type": qtype,
                "required": bool(f.get("required")), "is_legal_attestation": is_legal,
            }
            # Enumerated controls carry the choices the ATS will actually accept
            # (see `_FORM_RECON_JS.optionsFor`). `normalize_recon_questions`
            # sanitizes + caps them; `_needs_control_meta` prefers them over the
            # question-bank catalog, so the FE renders a real picker instead of a
            # free-text box the answer can never match.
            opts = f.get("options")
            if isinstance(opts, list) and opts:
                q["options"] = opts
            questions.append(q)
            if len(questions) >= 120:
                break
        return questions

    def probe_form(self, apply_url: str, *,
                   application_id: str = "probe") -> Dict[str, Any]:
        """Open an ATS apply URL, detect family + visible fields, screenshot.

        NEVER clicks Submit. Used for ops smoke against a live posting.
        For Greenhouse, navigates the same job-boards URL the submit driver uses
        and clicks Apply (if present) so the form DOM is visible — still no Submit.
        """
        from playwright.sync_api import sync_playwright
        from services import ssrf_guard
        from services.ats_adapters import select_adapter

        allowed, reason = ssrf_guard.apply_url_allowed(apply_url or "")
        if not allowed:
            return {"status": "failed", "error": f"ssrf_blocked:{reason}",
                    "ats": None, "field_count": 0, "screenshot_ref": None}

        family, ids = detect_ats(apply_url, None)
        # Prefer the same canonical apply URL the submit path uses.
        target = apply_url
        if family == GREENHOUSE:
            target = self._greenhouse_apply_url(ids, apply_url)
        elif family == LEVER:
            target = self._lever_apply_url(ids, apply_url)
        elif family == ASHBY:
            target = self._ashby_apply_url(ids, apply_url)

        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=self._headless,
                                             args=chromium_launch_args())
                try:
                    context = browser.new_context(**browser_context_kwargs())
                    ssrf_guard.install_route_guard(context)
                    page = context.new_page()
                    page.set_default_timeout(_FORM_TIMEOUT_MS)
                    page.goto(target, wait_until="domcontentloaded",
                              timeout=_NAV_TIMEOUT_MS)
                    try:
                        page.wait_for_load_state("networkidle", timeout=12_000)
                    except Exception:
                        page.wait_for_timeout(2_500)
                    adapter = select_adapter(page, target)
                    # Workday job-details → Apply (account gate often follows).
                    if adapter is not None and hasattr(adapter, "click_apply_cta"):
                        try:
                            if adapter.click_apply_cta():
                                page.wait_for_timeout(2_500)
                                try:
                                    page.wait_for_load_state("networkidle",
                                                             timeout=10_000)
                                except Exception:
                                    pass
                                adapter = select_adapter(page, page.url) or adapter
                        except Exception as e:
                            logger.info("probe workday apply CTA: %s", e)
                    # Modern Greenhouse job-boards show JD first; open the form.
                    for sel in (
                        "a:has-text('Apply')",
                        "button:has-text('Apply')",
                        "a[href*='application']",
                        "#apply_button",
                        "a[data-qa='btn-apply']",
                    ):
                        try:
                            loc = page.locator(sel).first
                            if loc.count() and loc.is_visible():
                                loc.click(timeout=5_000)
                                page.wait_for_timeout(1_500)
                                break
                        except Exception:
                            continue
                    try:
                        self._wait_for_form(page)
                    except Exception:
                        pass
                    if adapter is None:
                        adapter = select_adapter(page, page.url)
                    det = adapter.detect() if adapter else {}
                    fields = []
                    account_gate = False
                    otp_gate = False
                    try:
                        account_gate = bool(
                            getattr(adapter, "_account_gate_present", lambda: False)())
                        otp_gate = bool(
                            getattr(adapter, "_otp_gate_present", lambda: False)())
                    except Exception:
                        pass
                    try:
                        fields = adapter.recon() if adapter else []
                    except Exception as e:
                        logger.info("probe recon partial: %s", e)
                    # Fallback: raw input count when adapter is generic.
                    if not fields:
                        try:
                            raw = page.evaluate(_FORM_RECON_JS) or []
                            fields = [
                                {"id": f.get("name") or f.get("id"),
                                 "text": f.get("label") or f.get("name") or ""}
                                for f in raw if (f.get("name") or f.get("id"))
                            ]
                        except Exception:
                            pass
                    png = page.screenshot(full_page=False, type="png")
                    shot = self._save_screenshot(
                        {"application_id": application_id}, png)
                    title = ""
                    try:
                        title = page.title() or ""
                    except Exception:
                        pass
                    return {
                        "status": "ok",
                        "ats": (det or {}).get("ats") or family,
                        "confidence": (det or {}).get("confidence"),
                        "field_count": len(fields or []),
                        "fields_sample": [
                            {"id": q.get("id") or q.get("field_id"),
                             "text": (q.get("text") or q.get("label") or "")[:80],
                             "required": bool(q.get("required")),
                             "type": q.get("type")}
                            for q in (fields or [])[:40]
                        ],
                        "account_gate": account_gate,
                        "otp_gate": otp_gate,
                        "title": title[:200],
                        "url": page.url,
                        "ids": ids,
                        "screenshot_ref": shot,
                        "submitted": False,
                    }
                finally:
                    browser.close()
        except Exception as e:
            logger.warning("probe_form failed (%s): %s", apply_url, e)
            return {"status": "failed", "error": str(e)[:300],
                    "ats": family, "field_count": 0, "screenshot_ref": None,
                    "submitted": False}

    # -- submit (the real browser drive) ------------------------------------
    def submit(self, application: Dict[str, Any], answers: Dict[str, Any],
               resume_pdf: Any) -> Dict[str, Any]:
        apply_url = application.get("apply_url") or ""
        family, ids = detect_ats(apply_url, application.get("ats_family"))
        if family == GREENHOUSE:
            target, driver = self._greenhouse_apply_url(ids, apply_url), self._drive_greenhouse
        elif family == LEVER:
            target, driver = self._lever_apply_url(ids, apply_url), self._drive_lever
        elif family == ASHBY:
            target, driver = self._ashby_apply_url(ids, apply_url), self._drive_ashby
        elif family == WORKDAY:
            target, driver = apply_url, self._drive_workday
        else:
            # A vendor we cannot drive at all — PERMANENT, not a transient
            # drain. Reported as EXECUTOR_UNSUPPORTED so the orchestrator lands
            # ledger STATUS_UNSUPPORTED with an honest user message instead of
            # an opaque `executor_unavailable_midflight` failure (spec §4).
            logger.info("browser submit: family=%s not supported — unsupported vendor",
                        family)
            return self._unsupported(family)

        # user_email is folded into the résumé ref by _resume_ref (the executor
        # can't otherwise scope the S3 lookup); fall back to the constructor.
        ue = (resume_pdf.get("user_email") if isinstance(resume_pdf, dict) else None) \
            or self._user_email
        try:
            resume = self._resolve_resume(application, resume_pdf, ue)
        except Exception as e:
            logger.error("browser submit: résumé resolve failed: %s", e)
            return self._failed(f"resume_resolve: {e}")

        try:
            return driver(target, answers or {}, resume, application)
        except Exception as e:  # unexpected fault → failed (orchestrator marks ledger)
            if _CLOSED_RE.search(str(e)):
                # The page/context/browser died mid-drive (Chromium runs
                # --single-process in the Lambda sandbox). Nothing was
                # submitted; this is a TRANSIENT fleet fault to retry, not a
                # permanent application failure and not an opaque error.
                logger.warning("browser submit: browser/page closed mid-drive "
                               "(%s) — transient", str(e)[:120])
                return self._unavailable()
            logger.exception("browser submit: unexpected fault")
            return self._failed(str(e)[:300])

    # -- oracle hooks (Phase 2) ---------------------------------------------
    def create_account(self, user_email, ats_family, tenant, credentials) -> Dict[str, Any]:
        """Vault-backed Workday candidate account create/sign-in (server password).

        Uses ``ats_credential_service`` — passwords are always server-generated.
        ``credentials`` may supply ``username`` (managed mailbox alias preferred).

        ``resolve_login`` decides WHICH vault row backs this tenant: Workday is a
        shared-password family, so every tenant of a given user maps to that
        user's ONE canonical Workday secret — except a tenant that already owns
        a password from before that rule, which keeps it (the employer's side
        has that exact password). ``reveal`` follows the same pointer, so the
        password typed into the form always matches the stored row.
        """
        family = (ats_family or "").strip().lower()
        if family != WORKDAY:
            return {"status": EXECUTOR_UNAVAILABLE, "resolved": False,
                    "wall": None, "error": "create_account_unsupported_family"}
        user_email = (user_email or self._user_email or "").strip().lower()
        if not user_email:
            return {"status": EXECUTOR_UNAVAILABLE, "resolved": False,
                    "wall": None, "error": "missing_user_email"}
        tenant = (tenant or "default").strip().lower() or "default"
        username = ""
        login_url = ""
        company = ""
        if isinstance(credentials, dict):
            username = (credentials.get("username") or "").strip()
            login_url = (credentials.get("login_url") or "").strip()
            company = (credentials.get("company") or "").strip()
        if not username:
            username = self._managed_apply_email(user_email) or user_email
        try:
            from services import ats_credential_service as creds
            # Ensure the right vault row exists (never rotates an existing
            # password), then reveal through the single audited egress.
            resolved = creds.resolve_login(user_email, WORKDAY, tenant,
                                           username=username,
                                           login_url=login_url, company=company)
            revealed = creds.reveal(user_email, WORKDAY, tenant)
            return {
                "status": ORACLE_READY, "resolved": True, "wall": None, "error": None,
                "username": revealed.get("username") or username,
                "has_password": True,
                "tenant": tenant,
                "shared_password": bool(resolved.get("shared")),
                # Password stays in-process for the drive path; never logged.
                "_password": revealed.get("password"),
            }
        except Exception as e:
            logger.info("create_account vault failed: %s", e)
            return {"status": EXECUTOR_UNAVAILABLE, "resolved": False,
                    "wall": None, "error": str(e)[:200]}

    def _managed_apply_email(self, user_email: str) -> Optional[str]:
        """The managed private alias to register with. Provisions one when the
        user has none — falling back to the REAL address leaks it to the
        employer AND sends the ATS verification mail to an inbox this system
        cannot read, which is exactly how the OTP loop stalls."""
        try:
            from services import managed_mailbox_service as mailbox
            addr = mailbox.apply_alias(user_email)
            if not addr:
                logger.warning(
                    "workday account: no routable managed alias for this user "
                    "(APPLY_MAIL_DOMAIN unset?) — falling back to the account "
                    "address; ATS verification mail will not be readable")
            return addr or None
        except Exception:
            return None

    def _workday_tenant_from_url(self, url: str) -> str:
        try:
            from urllib.parse import urlparse
            host = (urlparse(url or "").hostname or "").lower()
            first = (host.split(".") or [""])[0]
            if first and first not in ("www", "careers", "wd1", "wd2", "wd3", "wd5"):
                return first
        except Exception:
            pass
        return "default"

    def _drive_workday(self, url, answers, resume, application) -> Dict[str, Any]:
        """Best-effort Workday wizard: Apply CTA → vault account → mailbox OTP → fill.

        Account passwords come only from ``ats_credential_service`` (server-
        generated). Managed mailbox alias is the candidate username so verify
        mail lands where we can read it.
        """
        from playwright.sync_api import sync_playwright
        from services import ssrf_guard
        from services.ats_adapters import select_adapter
        from services import fill_driver

        allowed, reason = ssrf_guard.apply_url_allowed(url or "")
        if not allowed:
            return self._failed(f"ssrf_blocked:{reason}")

        ue = (application.get("user_email") or self._user_email or "").strip()
        tenant = self._workday_tenant_from_url(url)
        resume_path = None

        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=self._headless,
                                             args=chromium_launch_args())
                try:
                    context = browser.new_context(**browser_context_kwargs())
                    ssrf_guard.install_route_guard(context)
                    page = context.new_page()
                    page.set_default_timeout(_FORM_TIMEOUT_MS)
                    page.goto(url, wait_until="domcontentloaded", timeout=_NAV_TIMEOUT_MS)
                    # Intel/WD SPAs hydrate Apply after shell paint; give them room.
                    try:
                        page.wait_for_load_state("networkidle", timeout=12_000)
                    except Exception:
                        page.wait_for_timeout(2_500)

                    def _already_applied() -> bool:
                        try:
                            body = (page.inner_text("body") or "").lower()
                        except Exception:
                            body = ""
                        return ("already applied" in body
                                or "you've already applied" in body
                                or "you have already applied" in body)

                    if _already_applied():
                        shot = None
                        try:
                            png = page.screenshot(full_page=False, type="png")
                            shot = self._save_screenshot(application, png)
                        except Exception:
                            pass
                        return {"status": SUBMIT_SUCCESS, "confirmation": True,
                                "screenshot_ref": shot, "wall": None, "error": None,
                                "already_applied": True}

                    adapter = select_adapter(page, url)
                    if adapter is None:
                        return self._unavailable()

                    # Job-details → Apply (cookie banner + SPA wait inside CTA).
                    try:
                        if hasattr(adapter, "click_apply_cta"):
                            if adapter.click_apply_cta():
                                page.wait_for_timeout(2_500)
                                try:
                                    page.wait_for_load_state("networkidle",
                                                             timeout=10_000)
                                except Exception:
                                    pass
                                adapter = select_adapter(page, page.url) or adapter
                    except Exception as e:
                        logger.info("workday apply CTA skipped: %s", e)

                    if _already_applied():
                        shot = None
                        try:
                            png = page.screenshot(full_page=False, type="png")
                            shot = self._save_screenshot(application, png)
                        except Exception:
                            pass
                        return {"status": SUBMIT_SUCCESS, "confirmation": True,
                                "screenshot_ref": shot, "wall": None, "error": None,
                                "already_applied": True}

                    # Account gate → vault create/sign-in + mailbox verify.
                    if getattr(adapter, "_account_gate_present", lambda: False)():
                        cleared = self._workday_clear_account_gate(
                            page, adapter, application, ue, tenant)
                        if not cleared:
                            return self._wall(WALL_VERIFY, page, application,
                                              ["account_required"])
                        page.wait_for_timeout(2_000)
                        adapter = select_adapter(page, page.url) or adapter
                        # Email-verify / OTP often follows create-account.
                        takeover = None
                        try:
                            takeover = adapter._check_takeover()
                        except Exception:
                            pass
                        if (getattr(adapter, "_otp_gate_present", lambda: False)()
                                or (takeover or {}).get("reason") == "mfa"):
                            self._try_mailbox_otp_on_page(page, application, timeout_sec=55)
                            page.wait_for_timeout(1_500)
                            adapter = select_adapter(page, page.url) or adapter

                    # Recover Workday "Something went wrong — please refresh" shells
                    # that appear after account gate / SPA navigations.
                    for _ in range(2):
                        try:
                            if hasattr(adapter, "recover_error_page") and adapter.recover_error_page():
                                page.wait_for_timeout(1_500)
                                adapter = select_adapter(page, page.url) or adapter
                                # If refresh dumped us back on the posting, re-open Apply.
                                if hasattr(adapter, "click_apply_cta") and adapter.click_apply_cta():
                                    page.wait_for_timeout(2_000)
                                    adapter = select_adapter(page, page.url) or adapter
                                    if getattr(adapter, "_account_gate_present", lambda: False)():
                                        self._workday_clear_account_gate(
                                            page, adapter, application, ue, tenant)
                                        adapter = select_adapter(page, page.url) or adapter
                            else:
                                break
                        except Exception as e:
                            logger.info("workday error-shell recover skipped: %s", e)
                            break

                    if resume and isinstance(resume, tuple) and len(resume) == 2:
                        import tempfile, os as _os
                        fd, resume_path = tempfile.mkstemp(suffix=".pdf")
                        _os.write(fd, resume[1])
                        _os.close(fd)
                        try:
                            adapter.upload_file("resume", resume_path)
                        except Exception as e:
                            logger.info("workday resume attach skipped: %s", e)

                    # Normalize the caller's key aliases and apply ONLY the
                    # defaults that assert nothing (see _workday_answers).
                    # Identity, employment history and EVERY legal attestation
                    # come from the resolved answers; anything missing stays
                    # missing so the driver parks at needs_input / waiting_human
                    # rather than inventing it (AUTOAPPLY_REPAIR_SPEC §0).
                    answers = _workday_answers(answers)

                    # Ops dry-run: fill the whole wizard but NEVER click Submit
                    # (copilot prepares only) — safe against a live posting.
                    dry_run = bool((application or {}).get("dry_run"))
                    mode = (fill_driver.MODE_COPILOT if dry_run
                            else fill_driver.MODE_AUTOPILOT)
                    outcome = fill_driver.drive(adapter, answers, mode=mode)
                    kind = (outcome or {}).get("outcome")

                    if kind == fill_driver.FILL_PREPARED:
                        # Filled + proof captured, deliberately NOT submitted.
                        return {"status": EXECUTOR_PREPARED, "confirmation": None,
                                "screenshot_ref": self._shoot(page, application),
                                "wall": None, "error": None,
                                "submitted": False, "dry_run": True,
                                "step": getattr(adapter, "current_step",
                                                lambda: None)()}

                    # One recovery pass for MFA / account mid-wizard.
                    if kind == fill_driver.FILL_WAITING_HUMAN:
                        reason = ((outcome or {}).get("reason")
                                  or (outcome or {}).get("takeover") or "human")
                        rlow = str(reason).lower()
                        field = (outcome or {}).get("field")
                        logger.info("workday waiting_human reason=%s field=%s", reason, field)
                        if "captcha" in rlow:
                            return self._wall(WALL_CAPTCHA, page, application, ["captcha"])
                        if "account" in rlow:
                            cleared = self._workday_clear_account_gate(
                                page, adapter, application, ue, tenant)
                            if cleared:
                                self._try_mailbox_otp_on_page(page, application, timeout_sec=45)
                                adapter = select_adapter(page, page.url) or adapter
                                outcome = fill_driver.drive(adapter, answers or {}, mode=mode)
                                kind = (outcome or {}).get("outcome")
                            else:
                                return self._wall(WALL_VERIFY, page, application,
                                                  ["account_required"])
                        elif "mfa" in rlow or "verify" in rlow:
                            if self._try_mailbox_otp_on_page(page, application, timeout_sec=55):
                                adapter = select_adapter(page, page.url) or adapter
                                outcome = fill_driver.drive(adapter, answers or {}, mode=mode)
                                kind = (outcome or {}).get("outcome")

                    if kind in (fill_driver.FILL_CONFIRMED, fill_driver.FILL_SUBMITTED):
                        shot = None
                        try:
                            png = page.screenshot(full_page=False, type="png")
                            shot = self._save_screenshot(application, png)
                        except Exception:
                            pass
                        return {"status": SUBMIT_SUCCESS, "confirmation": True,
                                "screenshot_ref": shot, "wall": None, "error": None}
                    if kind == fill_driver.FILL_WAITING_HUMAN:
                        reason = ((outcome or {}).get("reason")
                                  or (outcome or {}).get("takeover") or "human")
                        wall_kind = WALL_VERIFY
                        needs = [str(reason)[:120]]
                        if "captcha" in str(reason).lower():
                            wall_kind = WALL_CAPTCHA
                            needs = ["captcha"]
                        elif "account" in str(reason).lower():
                            needs = ["account_required"]
                        return self._wall(wall_kind, page, application, needs)
                    if kind == fill_driver.FILL_NEEDS_INPUT:
                        # Recover error shell mid-wizard then retry once.
                        try:
                            if hasattr(adapter, "recover_error_page") and adapter.recover_error_page():
                                adapter = select_adapter(page, page.url) or adapter
                                outcome = fill_driver.drive(adapter, answers or {}, mode=mode)
                                kind = (outcome or {}).get("outcome")
                                if kind in (fill_driver.FILL_CONFIRMED, fill_driver.FILL_SUBMITTED):
                                    shot = None
                                    try:
                                        png = page.screenshot(full_page=False, type="png")
                                        shot = self._save_screenshot(application, png)
                                    except Exception:
                                        pass
                                    return {"status": SUBMIT_SUCCESS, "confirmation": True,
                                            "screenshot_ref": shot, "wall": None, "error": None}
                        except Exception as e:
                            logger.info("workday mid-wizard recover skipped: %s", e)
                        # One more date/disability retry when Self Identify validation fails.
                        field = (outcome or {}).get("field") or ""
                        reason = (outcome or {}).get("reason") or ""
                        # REMOVED (AUTOAPPLY_REPAIR_SPEC §0 rules 1 + 4): the
                        # "Intel / Workday compliance" retry that used to live
                        # here was the LAST surviving fabrication path after
                        # `_WD_DEFAULTS` was deleted. On any `validation_failed`
                        # (or merely the word "secondary" appearing anywhere in
                        # the page body) it blind-clicked the first element whose
                        # text contained "Neither"/"No" — which on a Workday
                        # questionnaire is routinely the "No" option of "are you
                        # legally authorized to work…" / "will you require
                        # sponsorship…" — and then typed the literal
                        # "United States of America" into the first visible
                        # textarea as a countries-of-citizenship answer. Both are
                        # LEGAL ATTESTATIONS asserted with no declared user value,
                        # and both are false for the live user (F-1 OPT,
                        # citizenship India, sponsorship required).
                        #
                        # A legal attestation we do not have a declared value for
                        # must PARK, never be guessed: the run now falls through
                        # to the `needs_input` return below, which
                        # `auto_apply_service.finalize_submission` maps onto the
                        # ledger `needs_input` queue so the user is asked.
                        # Nothing may reintroduce a value here.
                        if "date" in str(field).lower() or reason == "validation_failed":
                            try:
                                from datetime import datetime, timezone
                                today = datetime.now(timezone.utc).strftime("%m/%d/%Y")
                                if hasattr(adapter, "_fill_month_year"):
                                    adapter._fill_month_year(
                                        "selfIdentifiedDisabilityData--dateSignedOn-dateSectionMonth-input",
                                        today)
                                if hasattr(adapter, "_fill_radio"):
                                    adapter._fill_radio(
                                        "formField-disabilityForm", "I Don't Wish To Answer")
                                # Also try page-wide disability text click.
                                try:
                                    page.get_by_text("I Don't Wish To Answer", exact=False).first.click(
                                        force=True, timeout=5_000)
                                except Exception:
                                    pass
                                outcome = fill_driver.drive(adapter, answers or {}, mode=mode)
                                kind = (outcome or {}).get("outcome")
                                if kind in (fill_driver.FILL_CONFIRMED, fill_driver.FILL_SUBMITTED):
                                    shot = None
                                    try:
                                        png = page.screenshot(full_page=False, type="png")
                                        shot = self._save_screenshot(application, png)
                                    except Exception:
                                        pass
                                    return {"status": SUBMIT_SUCCESS, "confirmation": True,
                                            "screenshot_ref": shot, "wall": None, "error": None}
                            except Exception as e:
                                logger.info("workday self-id retry failed: %s", e)
                        shot = None
                        try:
                            png = page.screenshot(
                                clip={"x": 0, "y": 0, "width": 1440, "height": 900})
                            shot = self._save_screenshot(application, png)
                        except Exception:
                            shot = self._shoot(page, application)
                        field = (outcome or {}).get("field")
                        reason = (outcome or {}).get("reason")
                        logger.info("workday needs_input field=%s reason=%s step=%s",
                                    field, reason,
                                    getattr(adapter, "current_step", lambda: "?")())
                        return {
                            "status": SUBMIT_FAILED,
                            "confirmation": None,
                            "screenshot_ref": shot,
                            "wall": None,
                            "error": "needs_input",
                            "field": field,
                            "reason": reason,
                            "step": getattr(adapter, "current_step", lambda: None)(),
                        }
                    if kind == fill_driver.FILL_SUBMISSION_UNKNOWN:
                        shot = None
                        body_snip = ""
                        try:
                            png = page.screenshot(
                                clip={"x": 0, "y": 0, "width": 1440, "height": 900})
                            shot = self._save_screenshot(application, png)
                        except Exception:
                            shot = self._shoot(page, application)
                        try:
                            body_snip = (page.inner_text("body") or "")[:400]
                        except Exception:
                            pass
                        logger.info(
                            "workday submission_unknown url=%s step=%s body=%s",
                            page.url,
                            getattr(adapter, "current_step", lambda: "?")(),
                            body_snip[:200],
                        )
                        return {"status": SUBMIT_FAILED, "confirmation": None,
                                "screenshot_ref": shot, "wall": None,
                                "error": "submission_unknown",
                                "url": page.url,
                                "step": getattr(adapter, "current_step", lambda: None)(),
                                "body_snip": body_snip}
                    return self._failed(f"workday_outcome:{kind}")
                finally:
                    browser.close()
                    if resume_path:
                        try:
                            import os as _os
                            _os.unlink(resume_path)
                        except Exception:
                            pass
        except Exception as e:
            logger.warning("workday drive failed: %s", e)
            return self._failed(str(e)[:300])

    def _workday_clear_account_gate(self, page, adapter, application,
                                    user_email: str, tenant: str) -> bool:
        """Sign in or create a Workday candidate account via the credential vault."""
        if not user_email:
            return False
        # PREFLIGHT: creating a Workday candidate account commits us to reading
        # a verification email. If this deployment cannot receive mail at a
        # managed alias, that round trip is impossible and the gate can never
        # clear — so registering an account is worse than not trying: it puts a
        # half-created account on a real employer's portal, burns the OTP
        # poll's full timeout inside a Lambda, and reports the dead end to the
        # user as though finishing it were their job.
        #
        # Measured on the live system: prod HAS both settings on
        # (`APPLY_MAIL_DOMAIN=my-private-mail.com`, `APPLY_MAILBOX_ENABLED=true`)
        # and Cloudflare MX is live, yet `mail_messages` holds nothing and four
        # Workday applications registered accounts against
        # `harshith.manne@my-private-mail.com` whose verification mail was never
        # observed arriving. Configuration alone therefore does NOT clear this
        # preflight — delivery has to have been seen for this user at least
        # once, or we are registering an account against an inbox we have no
        # evidence we can read.
        try:
            from services import managed_mailbox_service as mailbox
            if not mailbox.can_receive_mail():
                logger.info(
                    "workday account: managed mailbox not configured to receive "
                    "(APPLY_MAILBOX_ENABLED / APPLY_MAIL_DOMAIN) — not "
                    "registering an employer account we could never verify")
                return False
            if not mailbox.has_ever_received(user_email):
                logger.info(
                    "workday account: no mail has ever been observed arriving "
                    "for %s — not registering an employer account whose "
                    "verification link we have no evidence we can read",
                    user_email)
                return False
        except Exception:
            # A probe fault must not silently re-enable the flow.
            logger.warning("workday account: mailbox capability probe failed — "
                           "treating as unable to receive")
            return False
        apply_email = self._managed_apply_email(user_email) or user_email
        # login_url/company are DISPLAY metadata on the vault row so the user can
        # find this employer's portal again from the credentials screen.
        vault = self.create_account(
            user_email, WORKDAY, tenant,
            {"username": apply_email,
             "login_url": (application or {}).get("apply_url") or "",
             "company": (application or {}).get("company") or ""})
        if not vault.get("resolved") or not vault.get("_password"):
            logger.info("workday account: vault unavailable (%s)", vault.get("error"))
            return False
        password = vault["_password"]
        username = vault.get("username") or apply_email

        # Prefer create-account only when that panel is actually on screen.
        # applyManually URLs can still open a Sign In dialog after an account
        # already exists for the managed mailbox.
        prefer_create = False
        try:
            body_l = ((page.inner_text("body") or "")[:1500]).lower()
            has_verify_pw = (
                adapter._qs("[data-automation-id=confirmPassword]") is not None
                or adapter._qs("[data-automation-id=verifyPassword]") is not None
                or len(adapter._qsa("input[type=password]")) >= 2
            )
            signin_modal = (
                "sign in" in body_l
                and "create account" in body_l
                and not has_verify_pw
            )
            prefer_create = has_verify_pw and not signin_modal
        except Exception:
            prefer_create = False

        signed = False
        if hasattr(adapter, "fill_account_credentials"):
            # Prefer create when verify-password panel is showing; otherwise sign in.
            if prefer_create:
                signed = adapter.fill_account_credentials(
                    username, password, create=True)
                page.wait_for_timeout(3_000)
            else:
                signed = adapter.fill_account_credentials(
                    username, password, create=False)
                page.wait_for_timeout(4_000)
                try:
                    page.wait_for_load_state("networkidle", timeout=10_000)
                except Exception:
                    page.wait_for_timeout(1_500)
                still = getattr(adapter, "_account_gate_present", lambda: False)()
                # THE ACCOUNT-CREATION FIX: this used to also require
                # `"login" in page.url`. The wall we actually meet is served at
                # `.../apply/applyManually` (live: NXP, State Street, Midland
                # States Bank, 2026-07-28), so the guard was ALWAYS false, the
                # Create Account fallback never ran, and no employer account was
                # ever registered — the vault held a password the employer had
                # never seen. The honest signal is the gate still being on
                # screen after a sign-in attempt: the account does not exist (or
                # the password is wrong), and creating it is the next step.
                # The "already exists → sign in" recovery below closes the loop
                # if we were wrong.
                if still:
                    # Sign-in failed → open Create Account and provision.
                    try:
                        link = page.locator(
                            "a:has-text('Create Account'), "
                            "button:has-text('Create Account'), "
                            "[data-automation-id=createAccountLink]"
                        ).first
                        if link.count() and link.is_visible():
                            link.click(timeout=5_000)
                            page.wait_for_timeout(1_500)
                    except Exception:
                        pass
                    signed = adapter.fill_account_credentials(
                        username, password, create=True)
                    page.wait_for_timeout(3_000)

            # Intel: Create Account can pop a Sign In dialog (existing email).
            try:
                dlg = page.locator("[role=dialog]").first
                if dlg.count() and dlg.is_visible():
                    text = (dlg.inner_text() or "")[:300].lower()
                    if "sign in" in text:
                        signed = adapter.fill_account_credentials(
                            username, password, create=False)
                        page.wait_for_timeout(2_500)
            except Exception:
                pass

            # If create failed with "already exists", try sign-in once.
            try:
                body = (page.inner_text("body") or "")[:2500].lower()
                if any(s in body for s in (
                    "already exists", "already registered", "account already",
                    "existing account", "sign in instead",
                )):
                    signed = adapter.fill_account_credentials(
                        username, password, create=False)
                    page.wait_for_timeout(2_500)
            except Exception:
                pass

            # Intel: submitting Create Account when the mailbox already has an
            # account redirects to /login — sign in with the vault password.
            try:
                page.wait_for_timeout(1_000)
                still = getattr(adapter, "_account_gate_present", lambda: False)()
                url_l = (page.url or "").lower()
                title_l = ((page.title() or "")).lower()
                if still and ("/login" in url_l or title_l.startswith("sign in")):
                    if hasattr(adapter, "dismiss_overlays"):
                        adapter.dismiss_overlays()
                    signed = adapter.fill_account_credentials(
                        username, password, create=False)
                    page.wait_for_timeout(4_000)
                    try:
                        page.wait_for_load_state("networkidle", timeout=12_000)
                    except Exception:
                        page.wait_for_timeout(2_000)
            except Exception:
                pass

        # Drop password from locals as soon as used.
        password = None  # noqa: F841
        vault.pop("_password", None)

        try:
            self._shoot(page, application)
        except Exception:
            pass

        # Only poll mailbox when a verify/OTP wall is actually showing —
        # otherwise wait_for_grant burns the full timeout and Lambda dies.
        needs_otp = False
        try:
            needs_otp = bool(
                getattr(adapter, "_otp_gate_present", lambda: False)()
                or _VERIFY_RE.search(page.inner_text("body") or "")
            )
        except Exception:
            needs_otp = False
        if needs_otp:
            self._try_mailbox_otp_on_page(page, application, timeout_sec=55)
            page.wait_for_timeout(1_500)
        still = getattr(adapter, "_account_gate_present", lambda: True)()
        return not still

    def verify_state(self, application, freeze) -> Dict[str, Any]:
        """Re-drive a frozen verify/CAPTCHA wall (Oracle thaw sweep).

        Supports Greenhouse / Lever / Ashby / Workday page-clear checks. When the
        managed mailbox captured an OTP or ATS verify/reset link
        (``application_otp_service``), attempt to fill the code or follow the
        link before classifying the wall.
        """
        apply_url = application.get("apply_url") or ""
        family, ids = detect_ats(apply_url, application.get("ats_family"))
        if family == GREENHOUSE:
            target = self._greenhouse_apply_url(ids, apply_url)
        elif family == LEVER:
            target = self._lever_apply_url(ids, apply_url)
        elif family == ASHBY:
            target = self._ashby_apply_url(ids, apply_url)
        elif family == WORKDAY:
            target = apply_url
        else:
            return {"status": EXECUTOR_UNAVAILABLE, "resolved": False,
                    "wall": None, "error": None}

        # Claim any mailbox OTP/link for this application (best-effort).
        grant = None
        try:
            from services import application_otp_service as otp
            ue = (application.get("user_email") or self._user_email or "").strip()
            aid = application.get("application_id") or ""
            if ue and aid:
                grant = otp.claim_for_verify(ue, aid)
        except Exception as e:
            logger.info("verify_state otp claim skipped: %s", e)

        try:
            from playwright.sync_api import sync_playwright
            from services import ssrf_guard
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=self._headless,
                                             args=chromium_launch_args())
                try:
                    context = browser.new_context(**browser_context_kwargs())
                    ssrf_guard.install_route_guard(context)
                    page = context.new_page()
                    page.set_default_timeout(_FORM_TIMEOUT_MS)

                    # Prefer following a captured ATS verify/reset link.
                    nav = target
                    if grant and (grant.get("links") or []):
                        nav = grant["links"][0]
                    allowed, reason = ssrf_guard.apply_url_allowed(nav)
                    if not allowed and grant and grant.get("links"):
                        # Link host may be okta/auth0 — allow only if suffix check
                        # already passed in otp service; still require https.
                        from urllib.parse import urlparse
                        if (urlparse(nav).scheme or "").lower() != "https":
                            nav = target
                        else:
                            allowed = True
                    if not allowed:
                        nav = target

                    page.goto(nav, wait_until="domcontentloaded",
                              timeout=_NAV_TIMEOUT_MS)
                    try:
                        self._wait_for_form(page)
                    except Exception:
                        pass

                    if grant and grant.get("code"):
                        self._try_fill_otp(page, grant["code"])

                    if self._captcha_present(page):
                        return {"status": SUBMIT_WAITING_VERIFY, "resolved": False,
                                "wall": {"kind": WALL_CAPTCHA, "needs": ["captcha"]},
                                "error": None}
                    # Still showing a verify wall copy?
                    body = ""
                    try:
                        body = (page.inner_text("body") or "")[:4000]
                    except Exception:
                        pass
                    if _VERIFY_RE.search(body) and not (grant and grant.get("code")):
                        return {"status": SUBMIT_WAITING_VERIFY, "resolved": False,
                                "wall": {"kind": WALL_VERIFY,
                                         "needs": ["email_verification"]},
                                "error": None}
                    return {"status": ORACLE_READY, "resolved": True,
                            "wall": None, "error": None}
                finally:
                    browser.close()
        except Exception as e:
            logger.info("verify_state re-check failed (%s): %s", target, e)
            return {"status": EXECUTOR_UNAVAILABLE, "resolved": False,
                    "wall": None, "error": str(e)[:200]}

    def _try_fill_otp(self, page, code: str) -> bool:
        """Best-effort: drop an OTP into the most likely input on the page."""
        code = (code or "").strip()
        if not code:
            return False

        # Modern Greenhouse job-boards: 8 single-char boxes security-input-0..7.
        boxes = page.locator("input[id^='security-input-']")
        try:
            n = boxes.count()
        except Exception:
            n = 0
        if n >= 4:
            try:
                chars = list(code)[:n]
                page.evaluate(
                    """([ids, chars]) => {
                      const setter = Object.getOwnPropertyDescriptor(
                        window.HTMLInputElement.prototype, 'value').set;
                      for (let i = 0; i < chars.length; i++) {
                        const el = document.getElementById(ids[i]);
                        if (!el) continue;
                        setter.call(el, chars[i]);
                        el.dispatchEvent(new Event('input', { bubbles: true }));
                        el.dispatchEvent(new Event('change', { bubbles: true }));
                      }
                    }""",
                    ([f"security-input-{i}" for i in range(len(chars))], chars),
                )
                page.wait_for_timeout(500)
                for bsel in (
                    "button:has-text('Submit application')",
                    "button:has-text('Submit')",
                    "button:has-text('Verify')",
                    "button[type=submit]",
                ):
                    try:
                        btn = page.locator(bsel).first
                        if btn.count() and btn.is_visible():
                            btn.click(timeout=3_000)
                            page.wait_for_timeout(2_000)
                            break
                    except Exception:
                        continue
                return True
            except Exception as e:
                logger.info("security-input boxes fill failed: %s", e)

        # Workday / Okta OTP — data-automation-id hooks + digit boxes.
        for aid in (
            "otp", "otpCode", "otpInput", "verificationCode", "verifyCode",
            "securityCode", "emailVerificationCode", "oneTimePassword",
            "passcode", "mfaCode", "authCode",
        ):
            try:
                loc = page.locator(f"[data-automation-id='{aid}']").first
                if loc.count() and loc.is_visible():
                    loc.fill(code, timeout=3_000)
                    self._click_otp_confirm(page)
                    return True
            except Exception:
                continue
        try:
            wd_boxes = page.locator(
                "input[data-automation-id*=otp], "
                "input[data-automation-id*=Otp], "
                "input[data-automation-id*=verificationCode], "
                "input[aria-label*='code' i][maxlength='1']"
            )
            if wd_boxes.count() >= 4:
                chars = list(code)[: wd_boxes.count()]
                for i, ch in enumerate(chars):
                    wd_boxes.nth(i).fill(ch, timeout=2_000)
                self._click_otp_confirm(page)
                return True
        except Exception as e:
            logger.info("workday otp boxes fill failed: %s", e)

        # IMPORTANT: never fall back to a bare input[type=text] — that overwrites
        # first_name on Greenhouse and leaves the real security-code empty.
        selectors = (
            "input[autocomplete='one-time-code']",
            "input[name*='security' i]", "input[id*='security' i]",
            "input[placeholder*='security' i]", "input[aria-label*='security' i]",
            "input[name*='otp' i]", "input[id*='otp' i]",
            "input[name*='verification_code' i]", "input[id*='verification_code' i]",
            "input[name*='security_code' i]", "input[id*='security_code' i]",
            "input[name='code' i]", "input[id='code' i]",
            "input[placeholder*='code' i]",
            "input[aria-label*='code' i]",
            "input[name*='verify' i]", "input[id*='verify' i]",
        )
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                if loc.count() and loc.is_visible():
                    loc.fill(code, timeout=3_000)
                    for bsel in (
                        "button:has-text('Verify')",
                        "button:has-text('Continue')",
                        "button:has-text('Submit application')",
                        "button:has-text('Submit')",
                        "button[type=submit]",
                    ):
                        try:
                            btn = page.locator(bsel).first
                            if btn.count() and btn.is_visible():
                                btn.click(timeout=3_000)
                                page.wait_for_timeout(1_500)
                                break
                        except Exception:
                            continue
                    return True
            except Exception:
                continue
        # Label-associated: "Security code" → nearby input.
        try:
            lab = page.get_by_label(re.compile(r"security\s*code|verification\s*code|one[\s\-]?time", re.I))
            if lab.count():
                lab.first.fill(code, timeout=3_000)
                try:
                    page.locator("button:has-text('Submit application')").first.click(timeout=3_000)
                    page.wait_for_timeout(1_500)
                except Exception:
                    pass
                return True
        except Exception:
            pass
        return False

    def _click_otp_confirm(self, page) -> None:
        for bsel in (
            "button:has-text('Verify')",
            "button:has-text('Continue')",
            "button:has-text('Submit')",
            "button:has-text('Next')",
            "[data-automation-id=clickToVerify]",
            "[data-automation-id=authSelectSubmitButton]",
            "button[type=submit]",
        ):
            try:
                btn = page.locator(bsel).first
                if btn.count() and btn.is_visible():
                    btn.click(timeout=3_000)
                    page.wait_for_timeout(1_500)
                    return
            except Exception:
                continue

    # =======================================================================
    # Greenhouse form driver
    # =======================================================================
    def _greenhouse_apply_url(self, ids: Dict[str, Any], fallback: str) -> str:
        token, jid = ids.get("board_token"), ids.get("job_id")
        if token and jid:
            return f"https://job-boards.greenhouse.io/{token}/jobs/{jid}"
        return fallback

    def _lever_apply_url(self, ids: Dict[str, Any], fallback: str) -> str:
        site, pid = ids.get("site"), ids.get("posting_id")
        if site and pid:
            return f"https://jobs.lever.co/{site}/{pid}/apply"
        # A hosted Lever posting URL without /apply → append it (only for real
        # lever.co hosts; never rewrite an arbitrary/test URL).
        if fallback and "lever.co" in fallback and not fallback.rstrip("/").endswith("apply"):
            return fallback.rstrip("/") + "/apply"
        return fallback

    def _ashby_apply_url(self, ids: Dict[str, Any], fallback: str) -> str:
        org, pid = ids.get("org"), ids.get("posting_id")
        if org and pid:
            return f"https://jobs.ashbyhq.com/{org}/{pid}/application"
        if fallback and "ashbyhq.com" in fallback and "/application" not in fallback:
            return fallback.rstrip("/") + "/application"
        return fallback

    def _drive_greenhouse(self, url, answers, resume, application) -> Dict[str, Any]:
        return self._drive(url, answers, resume, application, self._fill_fields)

    def _drive_lever(self, url, answers, resume, application) -> Dict[str, Any]:
        return self._drive(url, answers, resume, application, self._fill_lever)

    def _drive_ashby(self, url, answers, resume, application) -> Dict[str, Any]:
        # Ashby fields carry ids (_systemfield_name / UUID) → id-based fill,
        # resolved from the live recon map (the ids are NOT the answer keys).
        return self._drive(url, answers, resume, application, self._fill_fields)

    def _drive(self, url: str, answers: Dict[str, Any],
               resume: Optional[Tuple[str, bytes]],
               application: Dict[str, Any], fill_fn) -> Dict[str, Any]:
        """Generic form-drive scaffolding shared across ATS families. ``fill_fn``
        is the family-specific field filler (Greenhouse/Ashby: id-keyed; Lever:
        `[name]` + native selects), and both take the recon-derived selector map
        built in ``_drive_page``. Everything else — nav, captcha gate, résumé
        attach, pre-submit gate, submit, classification, screenshot — is
        common."""
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=self._headless,
                                         args=chromium_launch_args())
            try:
                context = browser.new_context(**browser_context_kwargs())
                # P0-1 SSRF: for REAL http(s) navigation, intercept EVERY
                # request/redirect/subresource/popup/download/ws and abort
                # anything resolving to a private/loopback/link-local/metadata/
                # reserved IP (DNS-rebinding pinned), and require the top-level
                # target to be an https allow-listed ATS host. In production
                # submit() only ever hands _drive an https ATS URL, so this always
                # engages there; the offline test seam drives local ``file://``
                # fixtures (never a real apply target) and is left untouched.
                if (url or "").split(":", 1)[0].lower() in ("http", "https"):
                    from services import ssrf_guard
                    ssrf_guard.install_route_guard(context)
                    allowed, reason = ssrf_guard.apply_url_allowed(url)
                    if not allowed:
                        logger.warning("browser submit: apply_url blocked (%s)", reason)
                        return self._failed(f"blocked_url:{reason}")
                page = context.new_page()
                page.set_default_timeout(_FORM_TIMEOUT_MS)
                page.goto(url, wait_until="domcontentloaded", timeout=_NAV_TIMEOUT_MS)
                # Modern Greenhouse job-boards show JD first — open the form.
                for sel in (
                    "a:has-text('Apply')", "button:has-text('Apply')",
                    "a[href*='application']", "#apply_button",
                    "a[data-qa='btn-apply']",
                ):
                    try:
                        loc = page.locator(sel).first
                        if loc.count() and loc.is_visible():
                            loc.click(timeout=5_000)
                            page.wait_for_timeout(1_200)
                            break
                    except Exception:
                        continue
                return self._drive_page(page, url, answers, resume,
                                        application, fill_fn)
            finally:
                browser.close()

    def _drive_page(self, page, url: str, answers: Dict[str, Any],
                    resume: Optional[Tuple[str, bytes]],
                    application: Dict[str, Any], fill_fn) -> Dict[str, Any]:
        """The on-page half of ``_drive``: recon → fill → attach → PRE-SUBMIT
        GATE → submit → classify. Split out of ``_drive`` so the whole sequence
        is drivable against an offline DOM (``tests/ats_dom.FakePage``) — the
        browser lifecycle / SSRF / navigation stay in ``_drive``."""
        self._wait_for_form(page)

        # A challenge that's already up before we touch anything.
        # CAPTCHA strategy: try the (default no-op) solver seam first;
        # if it can't clear it, freeze → the user finishes in their own
        # browser (the human-in-the-loop primary path).
        if self._captcha_present(page) and not self._try_solve_captcha(page):
            return self._wall(WALL_CAPTCHA, page, application, ["captcha"])

        # Recon the form we are ACTUALLY on so every fill targets the element
        # recon found, instead of guessing `#<canonical field_id>`.
        fields = self._recon_live_fields(page, url)
        filled, skipped = fill_fn(page, answers, self._field_selectors(fields))
        resume_attached = self._attach_resume(page, resume) if resume else False

        # A page/context/browser that died under us (Lambda single-process
        # Chromium crash, target closed) makes every probe below meaningless —
        # and the fill silently no-opped. TRANSIENT: retry later, never a
        # ledger failure and never a submit.
        if self._page_dead(page):
            logger.warning("browser submit: page/browser closed mid-drive — "
                           "transient, nothing submitted")
            return self._unavailable()

        # SAFETY (spec §0 / fill_driver parity): a REQUIRED field we could not
        # put a value into stops the run BEFORE submit — never a half-filled
        # application to a real employer.
        gaps = self._presubmit_gaps(page, fields, answers, skipped,
                                    resume, resume_attached, filled=filled)

        # Ops dry-run: fill + proof shot, never click Submit (safe against
        # live employer postings).
        if (application or {}).get("dry_run"):
            ref = self._shoot(page, application)
            # NOT a failure: the form is filled and proof captured, the
            # Submit click was deliberately withheld (see
            # EXECUTOR_PREPARED / fill_driver.FILL_PREPARED).
            return {"status": EXECUTOR_PREPARED, "confirmation": None,
                    "screenshot_ref": ref, "wall": None, "error": None,
                    "filled": filled, "skipped": skipped, "gaps": gaps,
                    "submitted": False, "dry_run": True}

        if gaps:
            return self._needs_input(page, application, gaps,
                                     filled=filled, skipped=skipped)

        self._click_submit(page)
        page.wait_for_timeout(_SUBMIT_SETTLE_MS)

        outcome = self._classify_outcome(page)
        if outcome == "captcha":
            return self._wall(WALL_CAPTCHA, page, application, ["captcha"])
        if outcome == "verify":
            return self._handle_verify_outcome(
                page, application, ["email_verification"])
        if outcome == "success":
            ref = self._shoot(page, application)
            conf = self._confirmation_text(page)
            logger.info("browser submit SUCCESS (filled=%d skipped=%d)", filled, len(skipped))
            return {"status": SUBMIT_SUCCESS, "confirmation": conf,
                    "screenshot_ref": ref, "wall": None, "error": None}
        # Still on the form. Three different things look alike here and only
        # one of them is a failure:
        #   * an ANTI-BOT rejection ("flagged as possible spam") — a human
        #     challenge, and the row must reach the user's own browser;
        #   * an INVISIBLE reCAPTCHA-v3 score-block (no visible challenge, the
        #     submit silently doesn't go through) — same handoff;
        #   * a genuine field-level validation error — a real failure.
        antibot = self._antibot_block(page)
        if antibot:
            logger.info("browser submit: anti-bot rejection (%s) → human handoff",
                        antibot[:60])
            return self._wall(WALL_CAPTCHA, page, application, ["captcha"])
        field_err = self._field_validation_error(page)
        if field_err:
            ref = self._shoot(page, application)
            return {"status": SUBMIT_FAILED, "confirmation": None,
                    "screenshot_ref": ref, "wall": None, "error": field_err}
        if self._recaptcha_any(page):
            logger.info("browser submit: no confirmation + reCAPTCHA present "
                        "→ likely score-block, routing to human handoff")
            return self._wall(WALL_CAPTCHA, page, application, ["captcha"])
        ref = self._shoot(page, application)
        return {"status": SUBMIT_FAILED, "confirmation": None,
                "screenshot_ref": ref, "wall": None,
                "error": "no_confirmation_after_submit"}

    # -- recon-backed field targeting ----------------------------------------
    def _recon_live_fields(self, page, url: str) -> List[Dict[str, Any]]:
        """Recon the form ON SCREEN and return its normalized questions, each
        annotated with the live CSS ``selector`` of the element recon found.

        WHY (the Ashby incident): the vetted answers are keyed by the CANONICAL
        question-bank ``field_id`` (``full_name``, ``email``, …) that
        ``apply_forms.normalize_recon_questions`` derived from the question
        LABEL — never by the DOM handle, which is a per-vendor template
        (``_systemfield_name`` on one Ashby form, a question UUID on the next,
        ``cards[…][…]`` on Lever). Filling ``#<field_id>`` therefore matched
        nothing on Ashby and every field was silently skipped. Re-running the
        SAME normalization against the live DOM recovers the pairing
        field_id ↔ real handle, so the fill targets the element recon found.

        Deliberately re-reconned HERE rather than carried from the gather-phase
        recon: the answers are persisted flat (``auto_apply_service.
        _persist_answers``) and the ``ExecutorAdapter.submit`` contract passes
        ``{field_id: value}`` only, and a handle captured hours ago can be stale
        anyway. Never raises — an unusable recon degrades to the old id-guess.
        """
        try:
            from services.ats_adapters import select_adapter
            adapter = select_adapter(page, url or "")
            questions = adapter.recon() or []
        except Exception as e:
            logger.info("live recon unavailable (%s) — falling back to id-guess fill", e)
            return []
        fields: List[Dict[str, Any]] = []
        for q in questions:
            if not isinstance(q, dict):
                continue
            item = dict(q)
            item["selector"] = self._selector_for_handle(page, q.get("recon_id") or "")
            fields.append(item)
        return fields

    def _selector_for_handle(self, page, handle: str) -> Optional[str]:
        """A live CSS selector for the element recon reported under ``handle``
        (an id OR a name — Lever keys by name, Ashby/Greenhouse by id), or None
        when it is not on the page. Uses the ATTRIBUTE form (``[id="…"]``) so a
        handle that is not a valid CSS identifier — an Ashby question UUID
        starting with a digit — still addresses. No per-vendor prefixes."""
        if not handle:
            return None
        for sel in ('[id="%s"]' % _attr(handle), '[name="%s"]' % _attr(handle)):
            try:
                if page.query_selector(sel) is not None:
                    return sel
            except Exception:
                continue
        return None

    @staticmethod
    def _field_selectors(fields: List[Dict[str, Any]]) -> Dict[str, str]:
        """``{canonical field_id -> selector}``, plus the raw recon handle as an
        alias so a caller that keyed its answers by the ATS handle still hits."""
        out: Dict[str, str] = {}
        for q in fields or []:
            sel = q.get("selector")
            if not sel:
                continue
            for key in (q.get("field_id"), q.get("recon_id")):
                if key and key not in out:
                    out[str(key)] = sel
        return out

    def _page_dead(self, page) -> bool:
        """True when the page/context/browser is gone (Playwright: "Target page,
        context or browser has been closed"). Every read after that point is a
        lie — the fill silently no-ops and the drive must report a TRANSIENT
        unavailable rather than parking or submitting."""
        try:
            if bool(page.is_closed()):
                return True
        except Exception:
            pass
        try:
            page.query_selector("html")
            return False
        except Exception as e:
            return bool(_CLOSED_RE.search(str(e)))

    def _has_fillable_inputs(self, page) -> bool:
        """Whether this page presents any answerable control at all (used to tell
        'we filled nothing because the form is just an upload' from 'we filled
        nothing because every selector missed')."""
        for sel in ("input[type=text]", "input[type=email]", "input[type=tel]",
                    "input[type=url]", "textarea", "select"):
            try:
                if page.query_selector(sel) is not None:
                    return True
            except Exception:
                continue
        return False

    def _presubmit_gaps(self, page, fields: List[Dict[str, Any]],
                        answers: Dict[str, Any], skipped: List[str],
                        resume: Optional[Tuple[str, bytes]],
                        resume_attached: bool, *,
                        filled: int = 0) -> List[Dict[str, Any]]:
        """Every REQUIRED field this drive could not put a value into.

        This is the executor's copy of the rail ``fill_driver`` enforces for
        Workday (``FILL_NEEDS_INPUT`` / ``FILL_WAITING_HUMAN`` before any
        submit): a required field that is unanswered, or answered but whose
        element we could not locate/fill, STOPS the run and names the field.
        Greenhouse/Lever/Ashby bypass ``fill_driver`` entirely, so without this
        the drive happily clicked Submit on a form where ZERO fields were filled
        — exactly the production Ashby failure ("Missing entry for required
        field: Name / Email / Résumé"). Reasons mirror the fill_driver
        vocabulary so the ledger/one-tap queue reads identically.
        """
        gaps: List[Dict[str, Any]] = []
        skipped_keys = {str(s) for s in (skipped or [])}
        answered = {str(k) for k, v in (answers or {}).items()
                    if v not in (None, "")}

        # THE BACKSTOP, independent of recon quality: we put NOTHING into a form
        # that plainly has answerable controls. That is the signature of the
        # production incident (every selector missed → a pristine form clicked
        # Submit), and it is also what an ATS scores as a bot — two Ashby
        # postings answered our empty submissions with "flagged as possible
        # spam". Never submit an empty form to a real employer.
        if not filled and (fields or self._has_fillable_inputs(page)):
            gaps.append({"field": "form", "reason": "no_fields_filled"})
        for q in fields or []:
            if not q.get("required"):
                continue
            keys = {str(k) for k in (q.get("field_id"), q.get("recon_id")) if k}
            if not keys:
                continue
            name = str(q.get("field_id") or q.get("recon_id"))
            if keys & skipped_keys:
                # We HAD a vetted value and could not place it (handle renamed /
                # element gone / fill rejected).
                gaps.append({"field": name, "reason": "field_not_on_page"})
            elif not (keys & answered):
                # No vetted value at all — never guessed, so the form cannot be
                # completed by us. A legal/EEO attestation says so explicitly.
                gaps.append({"field": name,
                             "reason": ("sensitive_unconfirmed"
                                        if q.get("is_legal_attestation")
                                        else "unresolved")})
        # The résumé is required by every ATS this executor drives and is NOT in
        # `fields` (recon drops executor-supplied file handles), so check it
        # separately: a form that asks for one must never be submitted without.
        if not resume_attached and self._resume_input_selectors(page):
            gaps.append({"field": "resume",
                         "reason": "resume_not_attached" if resume
                                   else "resume_missing"})
        return gaps

    def _needs_input(self, page, application: Dict[str, Any],
                     gaps: List[Dict[str, Any]], *, filled: int = 0,
                     skipped: Optional[List[str]] = None) -> Dict[str, Any]:
        """Stop BEFORE submit and hand the named field back to the user.

        Same wire shape the Workday drive already returns for a fill_driver
        ``needs_input`` (SUBMIT_FAILED + ``error="needs_input"`` + ``field`` +
        ``reason``), which ``auto_apply_service`` parks in the one-tap
        ``needs_input`` queue rather than failing the row. ``submitted`` is
        always False — nothing was clicked."""
        first = gaps[0] if gaps else {"field": "field", "reason": "unresolved"}
        logger.info("browser submit BLOCKED pre-submit: %d required gap(s), "
                    "first=%s reason=%s (filled=%d)", len(gaps),
                    first.get("field"), first.get("reason"), filled)
        try:
            from services import apply_metrics
            apply_metrics.emit("presubmit_block", adapter=self.name,
                               outcome=str(first.get("reason")), count=len(gaps))
        except Exception:  # pragma: no cover - metrics never load-bearing
            pass
        return {"status": SUBMIT_FAILED, "confirmation": None,
                "screenshot_ref": self._shoot(page, application),
                "wall": None, "error": "needs_input",
                "field": first.get("field"), "reason": first.get("reason"),
                "gaps": gaps, "filled": filled, "skipped": list(skipped or []),
                "submitted": False}

    # -- form primitives -----------------------------------------------------
    def _wait_for_form(self, page) -> None:
        # First name or the submit button appearing = the form is interactive.
        try:
            page.wait_for_selector(
                "#first_name, input[autocomplete='given-name'], "
                "button:has-text('Submit application')", timeout=_FORM_TIMEOUT_MS)
        except Exception:
            pass  # proceed; individual fills will no-op if absent

    def _fill_fields(self, page, answers: Dict[str, Any],
                     field_map: Optional[Dict[str, str]] = None) -> Tuple[int, list]:
        """Fill an id-keyed form (Greenhouse / Ashby).

        ``field_map`` is the recon-derived ``{answer key -> live selector}`` map
        (see ``_recon_live_fields``) and is ALWAYS preferred: the answer keys are
        canonical field_ids, which equal the DOM id only by coincidence (they do
        on Greenhouse, they never do on Ashby's ``_systemfield_*`` / UUID
        handles). The ``#<key>`` guess remains only as the fallback for a page we
        could not recon."""
        filled, skipped = 0, []
        field_map = field_map or {}
        for fid, value in answers.items():
            if value is None or value == "":
                continue
            # recon map → the answer key AS a live id/name handle → the legacy
            # `#id` guess. The middle step matters on modern Greenhouse, where a
            # React-rendered control routinely carries `name="country"` with a
            # generated (or absent) id, so `#country` misses an element that
            # `[name="country"]` finds.
            sel = (field_map.get(str(fid))
                   or self._selector_for_handle(page, str(fid))
                   or f"#{_css_id(fid)}")
            try:
                el = page.query_selector(sel)
            except Exception:
                el = None
            if el is None:
                skipped.append(fid)
                continue
            try:
                tag = (el.evaluate("e => e.tagName") or "").lower()
                cls = (el.get_attribute("class") or "")
                role = (el.get_attribute("role") or "")
                if tag == "select":
                    self._fill_native_select(page, sel, fid, str(value))
                elif _SELECT_HINT in cls or role == "combobox" or tag == "select":
                    self._fill_select(page, sel, str(value), field_id=fid)
                else:
                    el.fill(str(value))
                filled += 1
            except Exception as e:
                logger.info("fill skip %s: %s", fid, e)
                skipped.append(fid)
        if skipped:
            # Selector-drift signal (spec §11): fields we expected to fill were
            # not found — the leading indicator of an ATS template change. PII-free
            # (COUNT only, never the field ids / values).
            try:
                from services import apply_metrics
                apply_metrics.emit("selector_drift", adapter=self.name,
                                   outcome="fields_missing", count=len(skipped))
            except Exception:  # pragma: no cover - metrics never load-bearing
                pass
        return filled, skipped

    def _fill_native_select(self, page, sel: str, field_id: str,
                            value: str) -> None:
        """Set a NATIVE ``<select>``.

        The form's own ``<option>`` labels are the truth, so match the declared
        value onto one of them first (``United States`` -> ``United States of
        America``); the raw label / value-attribute attempts stay as fallbacks.
        On no match nothing is selected and the exception propagates to the
        caller's skip handler — the field is left blank rather than mis-set.
        """
        attempts = []
        options = _native_option_labels(page, sel)
        if options:
            match = _match_form_option(field_id, value, options)
            if match:
                attempts.append(match)
        attempts.append(value)
        last: Optional[Exception] = None
        for label in attempts:
            try:
                page.select_option(sel, label=label)
                return
            except Exception as e:
                last = e
        try:
            page.select_option(sel, value)          # by value attribute
        except Exception:
            raise last if last is not None else RuntimeError("select failed")

    def _open_listbox(self, page, sel: str) -> List[Tuple[str, Any]]:
        """Open a div-based combobox and return the options it RENDERS.

        Three openers, in the order a human reaches for them, because no single
        one is reliable across builds:
          1. click the control (react-select opens on mousedown);
          2. ArrowDown on the focused input — react-select's own keyboard
             opener, and the only one that needs no hit-testing (the input is a
             ~2 px-wide grid cell inside `.select__input-container`);
          3. the vendor's own dropdown-indicator button, scoped to THIS control
             (Greenhouse renders `<button aria-label="Toggle flyout">`).

        Never raises: an unopenable widget returns [] and the caller asks."""
        try:
            page.click(sel, timeout=_CONTROL_CLICK_TIMEOUT_MS)
        except Exception:
            pass
        opts = _wait_for_listbox(page)
        if opts:
            return opts
        # (2) Keyboard. Kept in ONE try with the focus so a page object without a
        # keyboard surface is never poked at all — ArrowDown also HIGHLIGHTS the
        # first row, and a stray highlight on a gated field is not something to
        # leave behind.
        try:
            page.focus(sel)
            page.keyboard.press("ArrowDown")
        except Exception:
            pass
        opts = _rendered_options(page)
        if opts:
            return opts
        # (3) The vendor's own toggle.
        for xpath in _CONTROL_ANCESTOR_XPATHS:
            try:
                control = page.locator(sel).locator(xpath)
                if not control.count():
                    continue
                for btn in _TOGGLE_SELECTORS:
                    loc = control.locator(btn).first
                    if loc.count() and loc.is_visible():
                        loc.click(timeout=_OPTION_CLICK_TIMEOUT_MS)
                        return _wait_for_listbox(page)
            except Exception:
                continue
        return []

    def _click_matching_option(self, options: List[Tuple[str, Any]],
                               field_id: str, value: str) -> bool:
        """Click the rendered option that MEANS ``value``, or return False.

        The match runs through ``application_question_bank.match_option``, so
        geography is exact-or-same-cluster ("United States" == "United States of
        America" == "USA") and can never resolve to a neighbouring country. No
        confident match → False → the caller asks; never a nearest option."""
        if not options:
            return False
        match = _match_form_option(field_id, value, [t for t, _ in options])
        if not match:
            return False
        for i, (text, handle) in enumerate(options):
            if text != match:
                continue
            try:
                handle.click()
            except Exception as e:
                logger.info("fill_select: matched an option for %s but could "
                            "not click it: %s", field_id, e)
                return False
            logger.info("fill_select: %s ← matched option %d/%d from the open "
                        "listbox", field_id, i + 1, len(options))
            return True
        return False  # pragma: no cover - match came from this very list

    def _type_filter(self, page, sel: str, text: str) -> bool:
        """Type ``text`` into the combobox to narrow its listbox. False when the
        widget would not take keystrokes at all."""
        try:
            page.fill(sel, "")
            page.fill(sel, text)
            return True
        except Exception:
            pass
        try:
            page.type(sel, text, delay=40)
            return True
        except Exception:
            return False

    def _click_option_by_text(self, page, text: str) -> bool:
        """Last-resort SUBSTRING click for a listbox whose rows we could not
        enumerate. Caller-gated to non-enumerated fields only: `:has-text` is a
        substring match, so on a country list "India" also matches "British
        Indian Ocean Territory" — a different country, i.e. a fabrication."""
        for opt_sel in (
            f".select__option:has-text('{_q(text)}')",
            f"[role=option]:not(.iti__country):has-text('{_q(text)}')",
            f"li[role=option]:has-text('{_q(text)}')",
            f"div[class*='option']:has-text('{_q(text)}')",
        ):
            try:
                loc = page.locator(opt_sel).first
                if loc.count() and loc.is_visible():
                    loc.click(timeout=_OPTION_CLICK_TIMEOUT_MS)
                    return True
            except Exception:
                continue
        return False

    def _fill_select(self, page, sel: str, value: str,
                     field_id: str = "") -> None:
        """Drive react-select / native / autocomplete comboboxes.

        Native ``<select>`` first. Then the div-based combobox path, which is
        the production defect this shape exists for: a Greenhouse react-select
        materialises NO option until its listbox opens, so the old code typed a
        filter string and clicked blind by text — and when that missed (it did,
        for `country` and every gated screening field: prod
        `jobpilot-apply-executor-prod`, 2026-07-28) the field fell through to
        the refusal and the row parked. So: OPEN the listbox, READ the labels it
        renders, MATCH them through the question bank, CLICK the match. Typing
        to filter is now only a way to make a long list render fewer rows — the
        decision is always made on labels we actually read.
        """
        el = page.query_selector(sel)
        if el is not None:
            tag = (el.evaluate("e => e.tagName") or "").lower()
            if tag == "select":
                try:
                    page.select_option(sel, label=value)
                except Exception:
                    page.select_option(sel, value)
                return

        # ── 1. Open the listbox and decide on what it actually rendered. ─────
        options = self._open_listbox(page, sel)
        if self._click_matching_option(options, field_id, value):
            return

        # ── 2. Type-filter, then RE-READ. Alternate spellings come from the
        # question bank's synonym clusters (vendor-neutral — no adapter carries
        # its own "Greenhouse sometimes says United States of America" list).
        # Every candidate is another WORDING of the declared value, never a
        # different value, and the match below is still made against the
        # DECLARED value, not against the filter text.
        forbidden = _blind_pick_forbidden(field_id)
        candidates = _option_spellings(field_id, value)
        # Location widgets often return "City, ST, Country" — also try city-only.
        if "," in value:
            candidates.append(value.split(",", 1)[0].strip())

        for i, cand in enumerate(candidates[:_MAX_FILTER_CANDIDATES]):
            if not self._type_filter(page, sel, cand):
                break
            # Wait ONCE. A location autocomplete only populates after the first
            # keystrokes, so the first filter earns a wait; after that the
            # listbox is open and re-renders synchronously. Waiting per candidate
            # would spend `_LISTBOX_WAIT_MS` × 12 on a widget that renders
            # nothing — the exact budget burn this change is undoing.
            options = _wait_for_listbox(page) if i == 0 else _rendered_options(page)
            if self._click_matching_option(options, field_id, value):
                return
            # A listbox we could not enumerate at all. Substring-clicking it is
            # only safe where "a different option" is not "a different fact".
            if not forbidden and self._click_option_by_text(page, cand):
                return
        if options:
            # Log the LABELS, not just the count. A bare count says "no match"
            # without saying what we were matching against, so diagnosing a
            # mismatch meant a prod deploy to find out (this cost a full
            # round-trip on the Greenhouse `country` widget, 2026-07-29). The
            # labels are the one fact needed to tell "the widget renders text we
            # do not recognise" from "the synonym cluster is too narrow".
            # Option text is public form copy — no PII — and is truncated.
            logger.info("fill_select: read %d rendered option(s) for %s — none "
                        "means the declared value %r; options=%s",
                        len(options), field_id, value,
                        [label for label, _ in options[:12]])
        # ── BLIND FALLBACKS — NEVER for a gated or enumerated field ──────────
        #
        # Everything below picks whatever option happens to be FIRST. That is a
        # reasonable last resort for a location autocomplete ("San Francisco,
        # CA, USA" vs "San Francisco, California"), where the options are all
        # spellings of the same place.
        #
        # It is a FABRICATION for anything else. On a Greenhouse
        # work-authorization or sponsorship combobox, "first filtered option" is
        # an arbitrary answer to a legal attestation — and because this method
        # returns normally, the caller counts the field as `filled`, so the
        # pre-submit gate never sees a gap and the form is submitted with an
        # invented legal claim. That is spec §0 rule 1, and it is the same shape
        # as the two fabrication paths already deleted from this file
        # (`_WD_DEFAULTS`, and the validation-retry that blind-clicked "No").
        #
        # So: refuse. Returning without filling leaves the field in `skipped`,
        # `_presubmit_gaps` reports it, and the row parks as needs_input asking
        # the user — which is the correct answer when we genuinely do not know.
        if forbidden:
            logger.info(
                "fill_select: no option matched %r for %s — refusing the blind "
                "fallback on a gated/enumerated field", value, field_id)
            raise _BlindPickRefused(field_id)

        # First filtered option (common for location autocomplete).
        try:
            first = page.locator(
                ".select__option, [role=option]:not(.iti__country)"
            ).first
            if first.count() and first.is_visible():
                first.click(timeout=3_000)
                return
        except Exception:
            pass
        # Keyboard fallback — first filtered option.
        try:
            page.keyboard.press("ArrowDown")
            page.keyboard.press("Enter")
        except Exception:
            pass

    def _fill_lever(self, page, answers: Dict[str, Any],
                    field_map: Optional[Dict[str, str]] = None) -> Tuple[int, list]:
        """Lever fill: fields are keyed by the ``name`` attribute (not id), and
        selects are NATIVE ``<select>`` (not react-select), so this is simpler
        than Greenhouse. Handles text, native select, and checkbox/radio.

        As in ``_fill_fields``, the recon-derived ``field_map`` wins: a canonical
        answer key (``full_name``) is not Lever's ``name`` attribute (``name``,
        ``cards[…][…]``); the ``[name="<key>"]`` guess is the fallback only."""
        filled, skipped = 0, []
        field_map = field_map or {}
        for fid, value in answers.items():
            if value is None or value == "":
                continue
            sel = (field_map.get(str(fid))
                   or self._selector_for_handle(page, str(fid))
                   or f'[name="{_attr(fid)}"]')
            try:
                el = page.query_selector(sel)
            except Exception:
                el = None
            if el is None:
                skipped.append(fid)
                continue
            try:
                tag = (el.evaluate("e => e.tagName") or "").lower()
                typ = (el.get_attribute("type") or "").lower()
                if tag == "select":
                    self._fill_native_select(page, sel, fid, str(value))
                elif typ in ("checkbox", "radio"):
                    # Multiple same-name inputs → check the one matching value.
                    opt = page.query_selector(f'{sel}[value="{_attr(str(value))}"]')
                    (opt or el).check()
                else:
                    el.fill(str(value))
                filled += 1
            except Exception as e:
                logger.info("lever fill skip %s: %s", fid, e)
                skipped.append(fid)
        return filled, skipped

    def _resume_input_selectors(self, page) -> List[str]:
        """Live selectors for this form's résumé upload, best first, [] when the
        form has none.

        Found by WALKING the file inputs and reading their own attributes —
        never by a per-vendor id list (``#resume`` is Greenhouse's,
        ``#_systemfield_resume`` is ONE Ashby template of several, and the next
        one is a UUID). A file input that advertises itself as a cover letter is
        skipped; anything résumé-ish sorts first, other file inputs stay as a
        last resort (the historical behaviour)."""
        out: List[str] = []
        try:
            elements = page.query_selector_all("input[type=file]") or []
        except Exception:
            elements = []
        rest: List[str] = []
        for el in elements:
            hint, handle = "", ""
            try:
                for attr in ("id", "name", "aria-label", "data-testid",
                             "accept", "class", "placeholder"):
                    val = el.get_attribute(attr) or ""
                    if val and attr in ("id", "name") and not handle:
                        handle = val
                    hint += " " + val
            except Exception:
                continue
            sel = self._selector_for_handle(page, handle)
            if not sel:
                continue
            if _COVER_LETTER_RE.search(hint) and not _RESUME_RE.search(hint):
                continue
            (out if _RESUME_RE.search(hint) else rest).append(sel)
        # Unaddressable (no id/name) inputs still deserve the generic fallbacks.
        for sel in ("input[type=file][accept*='pdf']", "input[type=file]"):
            try:
                if page.query_selector(sel) is not None:
                    rest.append(sel)
            except Exception:
                continue
        for sel in rest:
            if sel not in out:
                out.append(sel)
        return out

    def _attach_resume(self, page, resume: Tuple[str, bytes]) -> bool:
        """Attach the résumé; returns whether a file actually went in. Prefer
        direct ``set_input_files`` (works headless even when the input is
        hidden); fall back to Attach → file chooser. A False here is a REQUIRED
        gap — ``_presubmit_gaps`` refuses to submit on it rather than sending a
        résumé-less application."""
        name, data = resume
        name = name or "resume.pdf"
        # A BASE upload may be .docx (tailored artifacts are always PDF) — declare
        # the type its extension actually implies or the ATS rejects the file.
        mime = ("application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                if name.lower().endswith((".docx", ".doc")) else "application/pdf")
        payload = {"name": name, "mimeType": mime, "buffer": data}
        # Direct input first — more reliable in Lambda than native file chooser.
        for sel in self._resume_input_selectors(page):
            try:
                page.set_input_files(sel, files=[payload])
                return True
            except Exception:
                continue
        attach = None
        for sel in ("button:has-text('Attach')", "label:has-text('Attach')",
                    "button:has-text('Upload file')", "[data-source=attach]"):
            try:
                attach = page.query_selector(sel)
            except Exception:
                attach = None       # dead/odd page → not an attach failure path
                break
            if attach is not None:
                break
        if attach is not None:
            try:
                with page.expect_file_chooser(timeout=_FORM_TIMEOUT_MS) as fc:
                    attach.click()
                fc.value.set_files(payload)
                return True
            except Exception as e:
                logger.info("attach via file-chooser failed (%s)", e)
        logger.info("resume attach: no usable file input on this form")
        return False

    def _click_submit(self, page) -> None:
        for sel in ("button:has-text('Submit application')",
                    "button[type=submit]", "input[type=submit]",
                    "button:has-text('Submit')"):
            el = page.query_selector(sel)
            if el is not None:
                el.click()
                return
        raise RuntimeError("submit button not found")

    # -- outcome classification ---------------------------------------------
    def _classify_outcome(self, page) -> str:
        if self._captcha_present(page):
            return "captcha"
        body = self._body_text(page)
        if _CONFIRM_RE.search(body) or self._confirm_element(page):
            return "success"
        if _VERIFY_RE.search(body):
            return "verify"
        return "unknown"

    def _try_solve_captcha(self, page) -> bool:
        """CAPTCHA-solver seam. Default = OFF (returns False) → the wall is
        frozen and handed to the user (human-in-the-loop, the chosen primary
        strategy). To enable a paid solver later (e.g. 2captcha/CapMonkey):
        gate on ``CAPTCHA_STRATEGY=solver`` + a configured API key, read the
        site key from the challenge iframe, submit it to the solver, wait for the
        token (holding the browser is fine — well within the 300 s timeout),
        inject it, and return True. Kept a no-op by design: cost + ToS + the fact
        that a logged-in human clears it more reliably than a headless bot."""
        strategy = (os.getenv("CAPTCHA_STRATEGY", "human") or "human").strip().lower()
        if strategy != "solver":
            return False
        logger.info("CAPTCHA_STRATEGY=solver set but no solver is wired — "
                    "falling back to human-in-the-loop")
        return False

    def _captcha_present(self, page) -> bool:
        """True only for a VISIBLE, sizable challenge frame — NOT the invisible
        reCAPTCHA-Enterprise/v3 anchor badge that rides silently on most forms
        and passes on score. Walling on the mere presence of the badge would
        falsely freeze every submission; a real image/checkbox challenge is a
        large visible iframe (the reCAPTCHA `bframe` / hCaptcha challenge)."""
        for sel in _CAPTCHA_SELECTORS:
            try:
                for el in page.query_selector_all(sel):
                    if not el.is_visible():
                        continue
                    box = el.bounding_box()
                    if box and box["width"] >= 50 and box["height"] >= 50:
                        return True
            except Exception:
                continue
        return False

    def _confirm_element(self, page):
        # A confirmation node that EXISTS but is hidden/empty is NOT a
        # confirmation — many forms keep an empty #confirmation div in the DOM
        # and only reveal it on real success. Require visible + non-empty.
        return self._visible_confirmation(page) is not None

    def _visible_confirmation(self, page):
        for sel in _CONFIRM_SELECTORS:
            try:
                el = page.query_selector(sel)
                if el is not None and el.is_visible() and (el.inner_text() or "").strip():
                    return el
            except Exception:
                continue
        return None

    def _confirmation_text(self, page) -> Optional[str]:
        el = self._visible_confirmation(page)
        if el is not None:
            return (el.inner_text() or "").strip()[:400] or "submitted"
        m = _CONFIRM_RE.search(self._body_text(page))
        return m.group(0)[:400] if m else "submitted"

    # An ATS's anti-bot verdict, which arrives in the SAME `[role=alert]` /
    # error-message slot a real field error uses. Two live Ashby submissions
    # (Cerebras, Chainalysis) came back with
    #
    #   "We couldn't submit your application. Your application submission was
    #    flagged as possible spam."
    #
    # and were recorded as terminal `failed` rows with that text as the error.
    # That is the wrong reading twice over: nothing about the application was
    # invalid, and the user was never offered the one-tap handoff that the
    # captcha path already builds for exactly this situation. It is a human
    # challenge, so it must produce a WALL.
    _ANTIBOT_RE = re.compile(
        r"(flagged as (?:possible )?spam|possible spam|suspected? (?:as )?spam"
        r"|automated (?:traffic|submission)s?|unusual (?:traffic|activity)"
        r"|bot(?:-| )?detect|are you a (?:human|robot)|verify you are human"
        r"|security check (?:failed|required)|access denied"
        r"|blocked for security|too many (?:attempts|requests)"
        r"|rate[- ]limit)", re.IGNORECASE)

    def _antibot_block(self, page) -> Optional[str]:
        """The visible text of an anti-bot rejection, or None.

        Checked BEFORE `_field_validation_error` because they read the same
        DOM slots and the anti-bot verdict is the more specific diagnosis.
        """
        for sel in (".field-error", "[class*=error-message]", "[role=alert]",
                    "[class*=alert]", "[class*=banner]"):
            try:
                el = page.query_selector(sel)
                if el is None or not el.is_visible():
                    continue
                txt = (el.inner_text() or "").strip()
                if txt and self._ANTIBOT_RE.search(txt):
                    return txt[:200]
            except Exception:
                continue
        return None

    def _field_validation_error(self, page) -> Optional[str]:
        """A genuine, VISIBLE field-level validation error (a control flagged
        invalid, or a shown error message) — NOT a hidden error placeholder that
        rides in the DOM of every Greenhouse form."""
        for sel in ("input[aria-invalid=true]", "select[aria-invalid=true]",
                    "textarea[aria-invalid=true]"):
            try:
                el = page.query_selector(sel)
                if el is not None and el.is_visible():
                    handle = el.get_attribute("id") or el.get_attribute("name") or "field"
                    return f"validation: {handle} required/invalid"
            except Exception:
                continue
        for sel in (".field-error", "[class*=error-message]", "[role=alert]"):
            try:
                el = page.query_selector(sel)
                if el is not None and el.is_visible():
                    txt = (el.inner_text() or "").strip()
                    if txt:
                        return f"validation: {txt[:160]}"
            except Exception:
                continue
        return None

    def _recaptcha_any(self, page) -> bool:
        """Any reCAPTCHA/hCaptcha on the page — INCLUDING the invisible v3 badge.
        Used only post-submit: if the submit didn't go through and this is
        present, a silent bot-score block is the likely cause."""
        for sel in ("iframe[src*=recaptcha]", "iframe[src*=hcaptcha]",
                    ".g-recaptcha", "[class*=grecaptcha]", "[data-sitekey]"):
            try:
                if page.query_selector(sel) is not None:
                    return True
            except Exception:
                continue
        return False

    def _body_text(self, page) -> str:
        try:
            return page.inner_text("body")
        except Exception:
            return ""

    # -- helpers -------------------------------------------------------------
    def _shoot(self, page, application) -> Optional[str]:
        """P1-SHOT: capture a BOUNDED confirmation-region proof, never a full-page
        screenshot of a filled form (which would store name / contact / work-auth
        / EEO / salary PII). Prefer the visible confirmation node; else a clipped
        top viewport strip (the banner/confirmation lives at the top). Only falls
        back to a viewport (NOT full_page) capture."""
        png = None
        try:
            el = self._visible_confirmation(page)
            if el is not None:
                png = el.screenshot()
        except Exception:
            png = None
        if png is None:
            try:
                png = page.screenshot(clip={"x": 0, "y": 0, "width": 1280, "height": 320})
            except Exception:
                try:
                    png = page.screenshot()   # viewport only — never full_page
                except Exception:
                    return None
        return self._save_screenshot(application, png)

    def _try_mailbox_otp_on_page(
        self,
        page,
        application: Dict[str, Any],
        *,
        timeout_sec: int = 90,
    ) -> bool:
        """Poll the managed mailbox and try to clear an email-verify / OTP wall."""
        ue = (application.get("user_email") or self._user_email or "").strip()
        aid = (application.get("application_id") or "").strip()
        if not ue:
            return False
        try:
            from services import application_otp_service as otp
            grant = otp.wait_for_grant(ue, aid, timeout_sec=timeout_sec)
        except Exception as e:
            logger.info("mailbox otp poll skipped: %s", e)
            return False
        if not grant:
            return False

        if grant.get("links"):
            try:
                from urllib.parse import urlparse
                from services import ssrf_guard
                nav = grant["links"][0]
                allowed, _reason = ssrf_guard.apply_url_allowed(nav)
                if not allowed and (urlparse(nav).scheme or "").lower() == "https":
                    allowed = True
                if allowed:
                    page.goto(nav, wait_until="domcontentloaded", timeout=_NAV_TIMEOUT_MS)
                    page.wait_for_timeout(2_000)
            except Exception as e:
                logger.info("mailbox verify link failed: %s", e)

        if grant.get("code"):
            self._try_fill_otp(page, grant["code"])
            page.wait_for_timeout(1_500)
            for sel in (
                "button:has-text('Verify')", "button:has-text('Continue')",
                "button:has-text('Submit')", "input[type=submit]",
            ):
                try:
                    btn = page.locator(sel).first
                    if btn.count() and btn.is_visible():
                        btn.click(timeout=3_000)
                        page.wait_for_timeout(1_500)
                        break
                except Exception:
                    continue

        body = ""
        try:
            body = (page.inner_text("body") or "")[:4000]
        except Exception:
            pass
        return not _VERIFY_RE.search(body)

    def _handle_verify_outcome(
        self,
        page,
        application: Dict[str, Any],
        needs: list,
    ) -> Dict[str, Any]:
        """After submit hit a verify wall: poll mailbox OTP, retry submit if cleared."""
        if self._try_mailbox_otp_on_page(page, application):
            try:
                self._click_submit(page)
                page.wait_for_timeout(_SUBMIT_SETTLE_MS)
            except Exception:
                pass
            outcome = self._classify_outcome(page)
            if outcome == "success":
                ref = self._shoot(page, application)
                conf = self._confirmation_text(page)
                return {"status": SUBMIT_SUCCESS, "confirmation": conf,
                        "screenshot_ref": ref, "wall": None, "error": None}
            if outcome == "captcha":
                return self._wall(WALL_CAPTCHA, page, application, ["captcha"])
            if outcome != "verify":
                ref = self._shoot(page, application)
                return {"status": SUBMIT_SUCCESS, "confirmation": None,
                        "screenshot_ref": ref, "wall": None, "error": None}
        return self._wall(WALL_VERIFY, page, application, needs)

    def _wall(self, kind: str, page, application, needs: list) -> Dict[str, Any]:
        # Diagnostics: taller clip so Workday Create Account / Sign In submit
        # and validation errors are visible in ops proofs.
        diag = (os.getenv("APPLY_EXECUTOR_DIAGNOSTICS", "") or "").strip().lower() in (
            "1", "true", "yes", "on")
        if diag and kind == WALL_VERIFY and "account" in " ".join(needs or []).lower():
            try:
                png = page.screenshot(clip={"x": 0, "y": 0, "width": 1440, "height": 900})
                ref = self._save_screenshot(application, png)
            except Exception:
                ref = self._shoot(page, application)
            try:
                logger.info(
                    "workday account wall url=%s buttons=%s body=%s",
                    (page.url or "")[:200],
                    page.evaluate(
                        """() => Array.from(document.querySelectorAll('button,a[role=button]'))
                           .slice(0,30).map(b => (b.innerText||b.getAttribute('aria-label')||'').trim().slice(0,60))
                           .filter(Boolean)""") ,
                    ((page.inner_text("body") or "")[:400]).replace("\n", " "),
                )
            except Exception:
                pass
            return {"status": SUBMIT_WAITING_VERIFY, "confirmation": None,
                    "screenshot_ref": ref, "wall": {"kind": kind, "needs": needs},
                    "error": None}
        ref = self._shoot(page, application)
        return {"status": SUBMIT_WAITING_VERIFY, "confirmation": None,
                "screenshot_ref": ref, "wall": {"kind": kind, "needs": needs},
                "error": None}

    def _failed(self, error: str) -> Dict[str, Any]:
        return {"status": SUBMIT_FAILED, "confirmation": None,
                "screenshot_ref": None, "wall": None, "error": error}

    def _unavailable(self) -> Dict[str, Any]:
        """TRANSIENT: the executor could not act right now → retry later."""
        return {"status": EXECUTOR_UNAVAILABLE, "confirmation": None,
                "screenshot_ref": None, "wall": None, "error": None}

    def _unsupported(self, family: Optional[str],
                     reason: Optional[str] = None) -> Dict[str, Any]:
        """PERMANENT: this employer's ATS is not one we can drive.

        Distinct from ``_unavailable`` in both meaning and handling: there is
        nothing to retry, and the user must be told plainly (``user_message``)
        rather than shown a technical error. ``error`` intentionally mirrors
        ``reason`` so an orchestrator that has not yet learned the new status
        still records a legible cause instead of a blank failure."""
        fam = (family or "").strip().lower() or "unknown"
        detail = reason or f"unsupported_ats:{fam}"
        return {"status": EXECUTOR_UNSUPPORTED, "confirmation": None,
                "screenshot_ref": None, "wall": None,
                "ats_family": fam, "reason": detail, "error": detail,
                "user_message": UNSUPPORTED_USER_MESSAGE,
                "retryable": False, "submitted": False}


def _css_id(fid: str) -> str:
    """Escape a field id for use in a `#id` selector (question_123 is safe; be
    defensive about odd characters)."""
    return re.sub(r"([^A-Za-z0-9_-])", r"\\\1", str(fid))


class _BlindPickRefused(Exception):
    """Raised instead of blind-picking an option for a gated/enumerated field.

    The caller treats it like any other fill failure: the field lands in
    ``skipped``, ``_presubmit_gaps`` reports it, and the row parks as
    needs_input rather than submitting an invented answer.
    """

    def __init__(self, field_id: str):
        self.field_id = field_id
        super().__init__(f"blind option pick refused for {field_id!r}")


def _blind_pick_forbidden(field_id: str) -> bool:
    """True when "just take the first option" would be a FABRICATION.

    The rule is deliberately inverted: ONLY a field the bank marks `safe` may be
    blind-picked. Everything else refuses.

      * `legal` / `protected` (i.e. `qb.is_gated`) — work authorization,
        sponsorship, criminal history, EEO. An arbitrary option is an invented
        legal attestation.
      * `sensitive` — e.g. `has_security_clearance`, `current_salary`. NOT gated,
        so an is_gated-only check would have let these through: "first option"
        on a clearance dropdown claims a clearance the user may not hold.
      * ENUMERATED (`qb.is_enumerated`) — country / state / citizenship. These
        are `safe` by sensitivity, but their options are distinct real-world
        places, so "first option" is simply a different country than declared.

    A location autocomplete stays allowed: it is `safe` and not enumerated, and
    its options are competing spellings of ONE place — which is the only case
    the fallback was ever justified for.

    FAILS CLOSED: unknown sensitivity, an unreadable bank, or an empty id all
    refuse. A missed fill parks the row and asks the user; a wrong one is
    submitted to a real employer under their name.
    """
    if not field_id:
        return True
    try:
        from services import application_question_bank as qb
    except Exception:  # pragma: no cover - defensive
        logger.warning(
            "fill_select: question bank unavailable — refusing blind option "
            "pick for %s (fail closed)", field_id)
        return True
    try:
        if qb.is_gated(field_id) or qb.is_enumerated(field_id):
            return True
        meta = qb.FIELD_INDEX.get(field_id)
        if not meta:
            # Not in the catalog at all — a vendor-custom question. We cannot
            # reason about it, but it is also not a known legal/geo field, so
            # allow the location-style fallback rather than burying the user in
            # needs_input for every bespoke dropdown.
            return False
        return meta.get("sensitivity") != qb.SENS_SAFE
    except Exception:  # pragma: no cover - defensive
        return True


# ---------------------------------------------------------------------------
# Option matching (question-bank backed, vendor-neutral).
# ---------------------------------------------------------------------------
def _option_spellings(field_id: str, value: str) -> List[str]:
    """Alternate SPELLINGS of the declared value to try on a widget whose options
    cannot be read (react-select / listbox). Declared value first. Same value in
    other words — never a different value."""
    try:
        from services import application_question_bank as qb
        return qb.option_candidates(field_id, value) or [value]
    except Exception:  # pragma: no cover - bank never load-bearing for a fill
        return [value]


#: Selectors for the options a div-based combobox RENDERS once its listbox is
#: open, most specific first. ``.iti__country`` is intl-tel-input's phone-prefix
#: list, which must never be mistaken for the form's own listbox. Only the FIRST
#: selector that renders anything is read — mixing two of them would blend a
#: stray page-level ``[role=option]`` into the control's real choice list.
_LISTBOX_OPTION_SELECTORS: Tuple[str, ...] = (
    ".select__option",
    "[role=option]:not(.iti__country)",
    "[role=listbox] li",
    "[class*='menu'] [class*='option']",
)
#: A country listbox renders ~250 rows; cap well above that but not unbounded.
_MAX_RENDERED_OPTIONS = 400
#: How long to wait for a listbox to render after we open (or type-filter) it.
#: Deliberately SHORT: this ran at 5 000 ms once per enumerated field, and on the
#: Greenhouse forms that never rendered `.select__option` at all it burned ~40 s
#: of the executor's Lambda budget doing nothing.
_LISTBOX_WAIT_MS = 1_500
#: How many alternate spellings to type-filter with before giving up. The bank
#: returns up to 12; each is a DOM round trip, and a widget that has not
#: rendered an option by the 6th spelling is not going to.
_MAX_FILTER_CANDIDATES = 6
_CONTROL_CLICK_TIMEOUT_MS = 5_000
_OPTION_CLICK_TIMEOUT_MS = 3_000
#: XPaths from the combobox INPUT up to the react-select control that owns the
#: dropdown indicator button (Greenhouse renders it `aria-label="Toggle flyout"`).
#: Vendor-neutral: `select__control` is the classNamePrefix convention, `-control`
#: the emotion-generated suffix every react-select build carries.
_CONTROL_ANCESTOR_XPATHS: Tuple[str, ...] = (
    "xpath=ancestor::*[contains(@class,'select__control')][1]",
    "xpath=ancestor::*[contains(@class,'-control')][1]",
)
_TOGGLE_SELECTORS: Tuple[str, ...] = (
    "button[aria-label*='Toggle' i]",
    ".select__dropdown-indicator",
    ".select__indicators button",
)


def _rendered_options(page) -> List[Tuple[str, Any]]:
    """``[(label, element)]`` for the options a combobox has RENDERED right now.

    A react-select puts no ``<option>`` in the DOM until its listbox opens, which
    is why recon reports ``options: None`` for a Greenhouse country/screening
    control and why the value can only be committed by CLICKING a rendered row.
    Reading them here is what lets ``application_question_bank.match_option`` do
    its job (exact-or-same-cluster for geography) instead of the executor
    guessing blind.

    Never raises — an unreadable widget is an empty list, i.e. "ask the user"."""
    out: List[Tuple[str, Any]] = []
    seen = set()
    for sel in _LISTBOX_OPTION_SELECTORS:
        try:
            handles = page.query_selector_all(sel) or []
        except Exception:
            continue
        for handle in handles:
            try:
                if not handle.is_visible():
                    continue
                text = " ".join((handle.inner_text() or "").split())[:200]
            except Exception:
                continue
            if not text or text in seen:
                continue
            seen.add(text)
            out.append((text, handle))
            if len(out) >= _MAX_RENDERED_OPTIONS:
                return out
        if out:
            break
    return out


def _wait_for_listbox(page) -> List[Tuple[str, Any]]:
    """``_rendered_options`` with one short wait for an async/animated listbox."""
    opts = _rendered_options(page)
    if opts:
        return opts
    try:
        page.wait_for_selector(", ".join(_LISTBOX_OPTION_SELECTORS[:2]),
                               timeout=_LISTBOX_WAIT_MS)
    except Exception:
        pass
    return _rendered_options(page)


def _native_option_labels(page, sel: str) -> List[str]:
    """The live ``<option>`` label texts of a native select ([] when unreadable)."""
    try:
        opts = page.query_selector_all(f"{sel} option") or []
    except Exception:
        return []
    out: List[str] = []
    for o in opts:
        try:
            t = (o.inner_text() or "").strip()
        except Exception:
            continue
        if t and t not in out:
            out.append(t)
    return out


def _match_form_option(field_id: str, value: str,
                       options: List[str]) -> Optional[str]:
    """The form's OWN option that means the declared value, or None. None means
    ask — never a fallback to a different option."""
    try:
        from services import application_question_bank as qb
        match = qb.match_option(field_id, value, options)
    except Exception:  # pragma: no cover - defensive
        return None
    return match["option"] if match else None


def _q(value: str) -> str:
    return str(value).replace("'", "\\'")


def _attr(value: str) -> str:
    """Escape a value for use inside a [name="..."] attribute selector."""
    return str(value).replace("\\", "\\\\").replace('"', '\\"')

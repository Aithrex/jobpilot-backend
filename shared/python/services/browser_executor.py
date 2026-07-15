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
    ``answers`` map (``{field_id: value}`` where ``field_id`` is the recon id,
    which on Greenhouse IS the DOM element id), attaches the résumé PDF, clicks
    submit, and classifies the outcome:
        SUBMIT_SUCCESS        confirmation detected  (+ screenshot_ref)
        SUBMIT_WAITING_VERIFY a CAPTCHA / email-verify wall  (freeze/thaw)
        SUBMIT_FAILED         hard validation/other failure
    It NEVER invents a value for a field not in ``answers`` and NEVER raises for
    an expected condition — every path returns a structured dict.
  * The résumé is resolved to bytes via an injectable ``resume_resolver`` (the
    default resolves ``tailoring_record_id`` → S3; tests inject bytes directly),
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
from typing import Any, Callable, Dict, Optional, Tuple

from services.ats_detector import detect_ats, GREENHOUSE, LEVER, ASHBY
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
  document.querySelectorAll('input, select, textarea').forEach(el => {
    const type = el.tagName === 'SELECT' ? 'select'
               : el.tagName === 'TEXTAREA' ? 'textarea'
               : (el.type || 'text').toLowerCase();
    if (['hidden','submit','button','reset'].includes(type)) return;
    const handle = el.id || el.name || '';
    if (!handle || seen.has(handle)) return;   // collapse radio/checkbox groups
    seen.add(handle);
    out.push({ name: handle, type, label: labelFor(el) || handle,
               required: el.required || el.getAttribute('aria-required') === 'true' });
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
    r"a verification (link|code) )", re.IGNORECASE)
# A react-select combobox on Greenhouse: input carrying this class / role.
_SELECT_HINT = "select__input"


# ---------------------------------------------------------------------------
# Injectable defaults (S3-backed) — kept import-light so tests need no AWS.
# ---------------------------------------------------------------------------
def _default_resume_resolver(application: Dict[str, Any],
                             resume_pdf: Any,
                             user_email: Optional[str]) -> Optional[Tuple[str, bytes]]:
    """Resolve ``{tailoring_record_id}`` → (filename, pdf_bytes) from S3.

    Returns None when no tailored résumé is linked (the caller then submits
    without a résumé attachment only if the form allows it). Never raises past
    the caller's try/except.
    """
    ref = resume_pdf or {}
    trid = ref.get("tailoring_record_id") if isinstance(ref, dict) else None
    if not trid:
        return None
    from utils.db_connect import DBConnect
    from services.s3_service import get_storage_service

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
        storage.client.put_object(Bucket=storage.bucket, Key=key, Body=png,
                                  ContentType="image/png")
        return key
    except Exception as e:  # pragma: no cover - audit is best-effort
        logger.warning("screenshot upload failed: %s", e)
        return None


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
        """Greenhouse recon is cheap HTTP (delegate to AtsExecutor). Lever/Ashby
        expose no public question API, so recon their form in the browser."""
        family, ids = detect_ats(apply_url, ats_family)
        if family == LEVER:
            return self._recon_browser(self._lever_apply_url(ids, apply_url),
                                       family, {"resume", "cover_letter"}, ("eeo",))
        if family == ASHBY:
            return self._recon_browser(self._ashby_apply_url(ids, apply_url),
                                       family, {"_systemfield_resume"}, ())
        return self._recon.recon_form(apply_url, ats_family)

    def _recon_browser(self, target: str, family: str, exclude_names: set,
                       legal_prefixes: tuple) -> Dict[str, Any]:
        """Generic browser recon for name/id-keyed forms (Lever, Ashby)."""
        from playwright.sync_api import sync_playwright
        from services.ats_executor import _is_legal
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
            questions.append({
                "id": name, "text": label, "type": qtype,
                "required": bool(f.get("required")), "is_legal_attestation": is_legal,
            })
            if len(questions) >= 120:
                break
        return {"questions": questions, "needs_account": False}

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
            # Ashby fields are id-keyed (like Greenhouse) → reuse the id-based fill.
            target, driver = self._ashby_apply_url(ids, apply_url), self._drive_ashby
        else:
            # Family without a driver yet (ashby/workday/…) → clean no-op; the
            # row stays ready for a later driver.
            logger.info("browser submit: family=%s not yet supported — unavailable", family)
            return self._unavailable()

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
            logger.exception("browser submit: unexpected fault")
            return self._failed(str(e)[:300])

    # -- oracle hooks (Phase 2) ---------------------------------------------
    def create_account(self, user_email, ats_family, tenant, credentials) -> Dict[str, Any]:
        return {"status": EXECUTOR_UNAVAILABLE, "resolved": False, "wall": None, "error": None}

    def verify_state(self, application, freeze) -> Dict[str, Any]:
        """Re-drive a frozen verify/CAPTCHA wall (Oracle thaw sweep). Cheap
        re-check: re-open the apply page and see whether a hard wall is still
        visibly blocking.

          * a visible challenge is STILL up  → resolved=False, keep frozen.
          * the form loads clean (no visible wall) → resolved=True + READY, so
            the orchestrator thaws the row and re-enqueues a full submit (which
            re-fills from stored answers and either lands or re-freezes).

        This is honest about CAPTCHA: a headless datacenter session can't *solve*
        a reCAPTCHA challenge, so a form that keeps challenging will keep
        re-freezing until attempts exhaust (→ the user is asked). But transient
        walls (a one-off verify, a challenge that isn't re-triggered) now clear
        automatically instead of the old always-`unavailable` dead-end."""
        apply_url = application.get("apply_url") or ""
        family, ids = detect_ats(apply_url, application.get("ats_family"))
        if family != GREENHOUSE:
            return {"status": EXECUTOR_UNAVAILABLE, "resolved": False,
                    "wall": None, "error": None}
        target = self._greenhouse_apply_url(ids, apply_url)
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=self._headless,
                                             args=chromium_launch_args())
                try:
                    page = browser.new_page()
                    page.set_default_timeout(_FORM_TIMEOUT_MS)
                    page.goto(target, wait_until="domcontentloaded",
                              timeout=_NAV_TIMEOUT_MS)
                    self._wait_for_form(page)
                    kind = (freeze or {}).get("kind") or WALL_CAPTCHA
                    if self._captcha_present(page):
                        return {"status": SUBMIT_WAITING_VERIFY, "resolved": False,
                                "wall": {"kind": WALL_CAPTCHA, "needs": ["captcha"]},
                                "error": None}
                    # No visible hard wall → worth a fresh submit attempt.
                    return {"status": ORACLE_READY, "resolved": True,
                            "wall": None, "error": None}
                finally:
                    browser.close()
        except Exception as e:
            logger.info("verify_state re-check failed (%s): %s", target, e)
            return {"status": EXECUTOR_UNAVAILABLE, "resolved": False,
                    "wall": None, "error": str(e)[:200]}

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
        # Ashby fields carry ids (_systemfield_name / UUID) → id-based fill.
        return self._drive(url, answers, resume, application, self._fill_fields)

    def _drive(self, url: str, answers: Dict[str, Any],
               resume: Optional[Tuple[str, bytes]],
               application: Dict[str, Any], fill_fn) -> Dict[str, Any]:
        """Generic form-drive scaffolding shared across ATS families. ``fill_fn``
        is the family-specific field filler (Greenhouse: `#id`; Lever: `[name]` +
        native selects). Everything else — nav, captcha gate, résumé attach,
        submit, outcome classification, screenshot — is common."""
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=self._headless,
                                         args=chromium_launch_args())
            try:
                context = browser.new_context(
                    user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                "AppleWebKit/537.36 (KHTML, like Gecko) "
                                "Chrome/124.0 Safari/537.36"))
                page = context.new_page()
                page.set_default_timeout(_FORM_TIMEOUT_MS)
                page.goto(url, wait_until="domcontentloaded", timeout=_NAV_TIMEOUT_MS)
                self._wait_for_form(page)

                # A challenge that's already up before we touch anything.
                # CAPTCHA strategy: try the (default no-op) solver seam first;
                # if it can't clear it, freeze → the user finishes in their own
                # browser (the human-in-the-loop primary path).
                if self._captcha_present(page) and not self._try_solve_captcha(page):
                    return self._wall(WALL_CAPTCHA, page, application, ["captcha"])

                filled, skipped = fill_fn(page, answers)
                if resume:
                    self._attach_resume(page, resume)

                self._click_submit(page)
                page.wait_for_timeout(_SUBMIT_SETTLE_MS)

                outcome = self._classify_outcome(page)
                if outcome == "captcha":
                    return self._wall(WALL_CAPTCHA, page, application, ["captcha"])
                if outcome == "verify":
                    return self._wall(WALL_VERIFY, page, application, ["email_verification"])
                if outcome == "success":
                    ref = self._shoot(page, application)
                    conf = self._confirmation_text(page)
                    logger.info("browser submit SUCCESS (filled=%d skipped=%d)", filled, len(skipped))
                    return {"status": SUBMIT_SUCCESS, "confirmation": conf,
                            "screenshot_ref": ref, "wall": None, "error": None}
                # Still on the form. Distinguish a genuine field error from an
                # INVISIBLE reCAPTCHA-v3 score-block (no visible challenge — the
                # submit just silently doesn't go through). A field error is a
                # real failure; a score-block is a captcha wall → human handoff.
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
            finally:
                browser.close()

    # -- form primitives -----------------------------------------------------
    def _wait_for_form(self, page) -> None:
        # First name or the submit button appearing = the form is interactive.
        try:
            page.wait_for_selector(
                "#first_name, input[autocomplete='given-name'], "
                "button:has-text('Submit application')", timeout=_FORM_TIMEOUT_MS)
        except Exception:
            pass  # proceed; individual fills will no-op if absent

    def _fill_fields(self, page, answers: Dict[str, Any]) -> Tuple[int, list]:
        filled, skipped = 0, []
        for fid, value in answers.items():
            if value is None or value == "":
                continue
            sel = f"#{_css_id(fid)}"
            try:
                el = page.query_selector(sel)
            except Exception:
                el = None
            if el is None:
                skipped.append(fid)
                continue
            try:
                cls = (el.get_attribute("class") or "")
                role = (el.get_attribute("role") or "")
                if _SELECT_HINT in cls or role == "combobox":
                    self._fill_select(page, sel, str(value))
                else:
                    el.fill(str(value))
                filled += 1
            except Exception as e:
                logger.info("fill skip %s: %s", fid, e)
                skipped.append(fid)
        return filled, skipped

    def _fill_select(self, page, sel: str, value: str) -> None:
        """Drive a react-select style combobox: focus, type to filter, click the
        matching option. Targets `.select__option` (Greenhouse react-select)
        FIRST so we don't accidentally hit the intl-tel-input country list."""
        page.click(sel)
        try:
            page.fill(sel, value)
        except Exception:
            page.type(sel, value)
        option = page.locator(
            f".select__option:has-text('{_q(value)}'), "
            f"[role=option]:not(.iti__country):has-text('{_q(value)}')"
        ).first
        option.click(timeout=_FORM_TIMEOUT_MS)

    def _fill_lever(self, page, answers: Dict[str, Any]) -> Tuple[int, list]:
        """Lever fill: fields are keyed by the ``name`` attribute (not id), and
        selects are NATIVE ``<select>`` (not react-select), so this is simpler
        than Greenhouse. Handles text, native select, and checkbox/radio."""
        filled, skipped = 0, []
        for fid, value in answers.items():
            if value is None or value == "":
                continue
            sel = f'[name="{_attr(fid)}"]'
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
                    try:
                        page.select_option(sel, label=str(value))
                    except Exception:
                        page.select_option(sel, str(value))
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

    def _attach_resume(self, page, resume: Tuple[str, bytes]) -> None:
        """Attach the résumé. Real Greenhouse hides the file input behind an
        "Attach" button that opens a native file chooser — drive that via
        expect_file_chooser; fall back to set_input_files on a plain input."""
        name, data = resume
        payload = {"name": name or "resume.pdf", "mimeType": "application/pdf", "buffer": data}
        # Preferred: the "Attach" button → native chooser.
        attach = None
        for sel in ("button:has-text('Attach')", "label:has-text('Attach')",
                    "button:has-text('Upload file')", "[data-source=attach]"):
            attach = page.query_selector(sel)
            if attach is not None:
                break
        if attach is not None:
            try:
                with page.expect_file_chooser(timeout=_FORM_TIMEOUT_MS) as fc:
                    attach.click()
                fc.value.set_files(payload)
                return
            except Exception as e:
                logger.info("attach via file-chooser failed (%s); trying direct input", e)
        # Fallback: a directly-settable file input (covers the test fixture and
        # simpler forms).
        for sel in ("#resume", "#_systemfield_resume",
                    "input[type=file][name=resume]", "input[type=file]"):
            if page.query_selector(sel) is not None:
                try:
                    page.set_input_files(sel, files=[payload])
                    return
                except Exception:
                    continue

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
        try:
            png = page.screenshot(full_page=True)
        except Exception:
            return None
        return self._save_screenshot(application, png)

    def _wall(self, kind: str, page, application, needs: list) -> Dict[str, Any]:
        ref = self._shoot(page, application)
        return {"status": SUBMIT_WAITING_VERIFY, "confirmation": None,
                "screenshot_ref": ref, "wall": {"kind": kind, "needs": needs},
                "error": None}

    def _failed(self, error: str) -> Dict[str, Any]:
        return {"status": SUBMIT_FAILED, "confirmation": None,
                "screenshot_ref": None, "wall": None, "error": error}

    def _unavailable(self) -> Dict[str, Any]:
        return {"status": EXECUTOR_UNAVAILABLE, "confirmation": None,
                "screenshot_ref": None, "wall": None, "error": None}


def _css_id(fid: str) -> str:
    """Escape a field id for use in a `#id` selector (question_123 is safe; be
    defensive about odd characters)."""
    return re.sub(r"([^A-Za-z0-9_-])", r"\\\1", str(fid))


def _q(value: str) -> str:
    return str(value).replace("'", "\\'")


def _attr(value: str) -> str:
    """Escape a value for use inside a [name="..."] attribute selector."""
    return str(value).replace("\\", "\\\\").replace('"', '\\"')

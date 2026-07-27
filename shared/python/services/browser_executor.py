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
        """Vault-backed Workday candidate account create/sign-in (server password).

        Uses ``ats_credential_service`` — passwords are always server-generated.
        ``credentials`` may supply ``username`` (managed mailbox alias preferred).
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
        if isinstance(credentials, dict):
            username = (credentials.get("username") or "").strip()
        if not username:
            username = self._managed_apply_email(user_email) or user_email
        try:
            from services import ats_credential_service as creds
            # Ensure vault row, then reveal. store_or_rotate is idempotent;
            # if reveal 404s after a race, store once more.
            try:
                revealed = creds.reveal(user_email, WORKDAY, tenant)
            except creds.CredentialError as e:
                if getattr(e, "error", "") != "not_found":
                    raise
                creds.store_or_rotate(user_email, WORKDAY, tenant, username=username)
                revealed = creds.reveal(user_email, WORKDAY, tenant)
            else:
                # Keep username aligned to managed mailbox when we already have a row.
                if username and revealed.get("username") != username:
                    pass  # username is metadata; password is what matters for sign-in

            return {
                "status": ORACLE_READY, "resolved": True, "wall": None, "error": None,
                "username": revealed.get("username") or username,
                "has_password": True,
                "tenant": tenant,
                # Password stays in-process for the drive path; never logged.
                "_password": revealed.get("password"),
            }
        except Exception as e:
            logger.info("create_account vault failed: %s", e)
            return {"status": EXECUTOR_UNAVAILABLE, "resolved": False,
                    "wall": None, "error": str(e)[:200]}

    def _managed_apply_email(self, user_email: str) -> Optional[str]:
        try:
            from services import managed_mailbox_service as mailbox
            mb = mailbox.get_mailbox(user_email) or {}
            addr = (mb.get("address") or "").strip()
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

                    # Enrich answers with Workday-common defaults that the
                    # caller's profile may not carry (radio/select fields that
                    # the wizard always asks but the user answer dict omits).
                    enriched = dict(answers or {})
                    from datetime import datetime, timezone
                    _today = datetime.now(timezone.utc).strftime("%m/%d/%Y")
                    _WD_DEFAULTS = {
                        "previous_employee": "No",
                        "phone_device_type": "Mobile",
                        "work_start_date": "01/2022",
                        "work_end_date": "06/2026",
                        "earliest_start_date": "01/2022",
                        "formfield_startdate": "01/2022",
                        "formfield_enddate": "06/2026",
                        "currently_work_here": "No",
                        "role_description": (
                            "Built and shipped full-stack AI career platform features "
                            "across React/TypeScript frontends and Flask/Python APIs, "
                            "including resume tooling, job matching, and automation flows."
                        ),
                        "job_description": (
                            "Built and shipped full-stack AI career platform features "
                            "across React/TypeScript frontends and Flask/Python APIs, "
                            "including resume tooling, job matching, and automation flows."
                        ),
                        "gender": "Decline",
                        "hispanic_latino": "No",
                        "race_ethnicity": "Decline to State (United States of America)",
                        "veteran_status": "I do not wish to provide this information",
                        "disability_status": "I Don't Wish To Answer",
                        "formfield_disabilityform": "I Don't Wish To Answer",
                        "formField-disabilityForm": "I Don't Wish To Answer",
                        "accept_terms": "Yes",
                        "formfield_accepttermsandagreements": "Yes",
                        "full_name": "Harshith Manne",
                        "name": "Harshith Manne",
                        "language": "English",
                        "formfield_datesignedon": _today,
                        "date_signed": _today,
                        "work_authorized": "Yes",
                        "work_authorized_us": "Yes",
                        "requires_sponsorship": "No",
                        "require_sponsorship": "No",
                        "over_18": "Yes",
                        "age_18_or_older": "Yes",
                        "ey_employee": "No",
                        "family_member_auditor": "No",
                        "noncompete_agreement": "No",
                        "own_ip_rights": "No",
                        "government_employee": "No",
                        "dod_employee": "No",
                        "export_control_us_person": "Yes",
                        "secondary_employment_intent": "Neither",
                        "citizenship_countries": "United States of America",
                        "countries_of_citizenship": "United States of America",
                    }
                    # Common key aliases (caller may use either form).
                    _WD_ALIASES = {
                        "address_line1": "address_line_1",
                        "current_title": "job_title",
                        "current_company": "company",
                        "formfield_country": "country",
                        "formfield_datesignedon": "date_signed",
                        "formfield_disabilityform": "disability_status",
                    }
                    for canon, alias in _WD_ALIASES.items():
                        if canon not in enriched and alias in enriched:
                            enriched[canon] = enriched[alias]
                    for k, v in _WD_DEFAULTS.items():
                        if k not in enriched:
                            enriched[k] = v
                    answers = enriched

                    mode = "autopilot"
                    outcome = fill_driver.drive(adapter, answers, mode=mode)
                    kind = (outcome or {}).get("outcome")

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
                        errors = (outcome or {}).get("errors") or []
                        err_blob = " ".join(
                            str((e or {}).get("message") or "") for e in errors
                        ).lower()
                        body_l = ""
                        try:
                            body_l = (page.inner_text("body") or "").lower()
                        except Exception:
                            pass
                        # Intel / Workday compliance: secondary employment + citizenship.
                        if ("secondary" in err_blob or "secondary" in body_l
                                or "citizenship" in err_blob or reason == "validation_failed"):
                            try:
                                for label in ("Neither", "Neither/Ninguno", "No"):
                                    try:
                                        loc = page.get_by_text(label, exact=False).first
                                        if loc.count():
                                            loc.click(force=True, timeout=4_000)
                                            break
                                    except Exception:
                                        pass
                                # Citizenship countries (not Yes/No).
                                try:
                                    cit = page.locator(
                                        "textarea, input[type=text]"
                                    ).filter(has_text="")
                                    # Prefer a field near citizenship wording via JS clear+type.
                                    for sel in (
                                        "textarea",
                                        "[data-automation-id*=citizenship] input",
                                        "[data-automation-id*=citizenship] textarea",
                                    ):
                                        try:
                                            loc = page.locator(sel).first
                                            if loc.count() and loc.is_visible():
                                                # Only overwrite if current value looks like Yes/No.
                                                cur = ""
                                                try:
                                                    cur = (loc.input_value() or "").strip().lower()
                                                except Exception:
                                                    cur = ""
                                                if cur in ("", "yes", "no", "yes/si", "si"):
                                                    loc.fill("United States of America")
                                                    break
                                        except Exception:
                                            pass
                                except Exception:
                                    pass
                                page.wait_for_timeout(800)
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
                                logger.info("workday compliance retry failed: %s", e)
                        if "date" in str(field).lower() or reason == "validation_failed":
                            try:
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
        apply_email = self._managed_apply_email(user_email) or user_email
        vault = self.create_account(user_email, WORKDAY, tenant,
                                    {"username": apply_email})
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
                on_login = "login" in (page.url or "").lower()
                if still and on_login:
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

                # Ops dry-run: fill + proof shot, never click Submit (safe against
                # live employer postings).
                if (application or {}).get("dry_run"):
                    ref = self._shoot(page, application)
                    return {"status": "prepared", "confirmation": None,
                            "screenshot_ref": ref, "wall": None, "error": None,
                            "filled": filled, "skipped": skipped, "submitted": False}

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
                tag = (el.evaluate("e => e.tagName") or "").lower()
                cls = (el.get_attribute("class") or "")
                role = (el.get_attribute("role") or "")
                if tag == "select":
                    try:
                        page.select_option(sel, label=str(value))
                    except Exception:
                        page.select_option(sel, str(value))
                elif _SELECT_HINT in cls or role == "combobox" or tag == "select":
                    self._fill_select(page, sel, str(value))
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

    def _fill_select(self, page, sel: str, value: str) -> None:
        """Drive react-select / native / autocomplete country comboboxes.

        Prefer native ``<select>``, then type-filter + click option (Greenhouse
        react-select). Avoids intl-tel-input country list. Falls back to
        ArrowDown+Enter when the option list uses a non-standard selector.
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

        page.click(sel, timeout=5_000)
        try:
            page.fill(sel, "")
            page.fill(sel, value)
        except Exception:
            try:
                page.type(sel, value, delay=40)
            except Exception:
                pass

        # Give async location/geo option lists a moment to populate.
        try:
            page.wait_for_selector(
                ".select__option, [role=option]:not(.iti__country)",
                timeout=5_000,
            )
        except Exception:
            pass

        # Greenhouse sometimes labels US as "United States of America".
        candidates = [value]
        if value.lower() in ("united states", "usa", "us"):
            candidates += ["United States of America", "United States", "USA"]
        # Location widgets often return "City, ST, Country" — also try city-only.
        if "," in value:
            candidates.append(value.split(",", 1)[0].strip())

        for cand in candidates:
            for opt_sel in (
                f".select__option:has-text('{_q(cand)}')",
                f"[role=option]:not(.iti__country):has-text('{_q(cand)}')",
                f"li[role=option]:has-text('{_q(cand)}')",
                f"div[class*='option']:has-text('{_q(cand)}')",
            ):
                try:
                    loc = page.locator(opt_sel).first
                    if loc.count() and loc.is_visible():
                        loc.click(timeout=3_000)
                        return
                except Exception:
                    continue
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
        """Attach the résumé. Prefer direct ``set_input_files`` (works headless
        even when the input is hidden); fall back to Attach → file chooser."""
        name, data = resume
        payload = {"name": name or "resume.pdf", "mimeType": "application/pdf", "buffer": data}
        # Direct input first — more reliable in Lambda than native file chooser.
        for sel in ("#resume", "#_systemfield_resume",
                    "input[type=file][name=resume]",
                    "input[type=file][accept*='pdf']",
                    "input[type=file]"):
            if page.query_selector(sel) is not None:
                try:
                    page.set_input_files(sel, files=[payload])
                    return
                except Exception:
                    continue
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
                logger.info("attach via file-chooser failed (%s)", e)

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

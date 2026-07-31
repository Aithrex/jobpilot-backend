"""Container-image Lambda handler for the auto-apply browser fleet.

This is the ONLY place a real browser runs. The resume Lambda (orchestrator)
reaches it via ``LambdaInvokeExecutor`` with a ``{"method","args"}`` payload;
this handler dispatches to the in-process ``BrowserExecutor`` (Playwright +
Chromium, shipped in the container image) and returns its structured dict
verbatim. Errors are swallowed into the contract's clean shapes so the caller
never sees an exception — a bad call just reads as unavailable/failed.

Runtime: Amazon Linux / Playwright base image, Chromium preinstalled, the
shared layer code mounted on PYTHONPATH (see the Dockerfile). Memory ~2 GB,
timeout 300 s, reserved concurrency small (cost + politeness cap).
"""
import logging
import os

from services.browser_executor import BrowserExecutor

logging.getLogger().setLevel(logging.INFO)
logger = logging.getLogger("apply_executor")

_executor = None


def _ex() -> BrowserExecutor:
    global _executor
    if _executor is None:
        # enabled=True: this container only exists to actually submit.
        _executor = BrowserExecutor(enabled=True)
    return _executor


def _diagnostics_enabled() -> bool:
    raw = (os.getenv("APPLY_EXECUTOR_DIAGNOSTICS", "") or "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def handler(event, context):
    method = (event or {}).get("method")
    args = (event or {}).get("args") or {}
    ex = _ex()
    try:
        if method == "recon_form":
            return ex.recon_form(args.get("apply_url", ""), args.get("ats_family"))
        if method == "submit":
            return ex.submit(args.get("application") or {}, args.get("answers") or {},
                             args.get("resume_pdf"))
        if method == "create_account":
            return ex.create_account(args.get("user_email"), args.get("ats_family"),
                                     args.get("tenant"), args.get("credentials") or {})
        if method == "verify_state":
            return ex.verify_state(args.get("application") or {}, args.get("freeze") or {})
        if method == "is_available":
            return {"available": ex.is_available()}
        if method == "submit_test":
            # Validation-only controlled form. Requires diagnostics flag ON.
            if not _diagnostics_enabled():
                return {"status": "failed", "error": "diagnostics_disabled",
                        "confirmation": None, "screenshot_ref": None, "wall": None}
            import base64
            from services.ats_detector import detect_ats, GREENHOUSE, LEVER, ASHBY, WORKDAY
            url = args.get("url") or ""
            answers = args.get("answers") or {}
            application = args.get("application") or {"application_id": "submit-test"}
            application = dict(application)
            if not application.get("apply_url"):
                application["apply_url"] = url
            if args.get("dry_run"):
                application["dry_run"] = True
            resume = None
            rb = args.get("resume_b64")
            if rb:
                resume = (args.get("resume_name") or "resume.pdf", base64.b64decode(rb))
            family, _ids = detect_ats(
                application.get("apply_url") or url, application.get("ats_family"))
            if family == WORKDAY:
                return ex._drive_workday(url, answers, resume, application)
            if family == LEVER:
                return ex._drive_lever(url, answers, resume, application)
            if family == ASHBY:
                return ex._drive_ashby(url, answers, resume, application)
            return ex._drive_greenhouse(url, answers, resume, application)
        if method == "submit_fixture":
            # Full fill→submit against in-memory Greenhouse-like HTML (no employer).
            if not _diagnostics_enabled():
                return {"status": "failed", "error": "diagnostics_disabled"}
            import base64
            from playwright.sync_api import sync_playwright
            from services.browser_executor import chromium_launch_args
            html = args.get("html") or ""
            answers = args.get("answers") or {
                "first_name": "Test", "last_name": "Applicant",
                "email": "test@example.com", "phone": "5550100",
                "question_100": "Yes",
            }
            resume_b64 = args.get("resume_b64")
            resume_bytes = base64.b64decode(resume_b64) if resume_b64 else (
                b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
            )
            application = {"application_id": args.get("application_id") or "fixture-submit"}
            with sync_playwright() as p:
                b = p.chromium.launch(headless=True, args=chromium_launch_args())
                try:
                    page = b.new_page()
                    page.set_content(html, wait_until="domcontentloaded")
                    filled, skipped = ex._fill_fields(page, answers)
                    ex._attach_resume(page, ("resume.pdf", resume_bytes))
                    ex._click_submit(page)
                    page.wait_for_timeout(500)
                    outcome = ex._classify_outcome(page)
                    ref = ex._shoot(page, application)
                    return {
                        "status": "submitted" if outcome == "success" else outcome,
                        "filled": filled, "skipped": skipped,
                        "screenshot_ref": ref, "submitted": outcome == "success",
                        "confirmation": ex._confirmation_text(page) if outcome == "success" else None,
                    }
                finally:
                    b.close()
        if method == "probe_form":
            # Open a real ATS apply URL, detect family + fields, screenshot —
            # NEVER click submit. Safe ops check against a live posting.
            if not _diagnostics_enabled():
                return {"status": "failed", "error": "diagnostics_disabled"}
            return ex.probe_form(args.get("apply_url", ""),
                                 application_id=args.get("application_id") or "probe")
        if method == "selftest":
            # Prove Chromium boots inside the Lambda sandbox (no employer contact).
            if not _diagnostics_enabled():
                return {"status": "failed", "error": "diagnostics_disabled"}
            from playwright.sync_api import sync_playwright
            from services.browser_executor import chromium_launch_args
            with sync_playwright() as p:
                b = p.chromium.launch(headless=True, args=chromium_launch_args())
                pg = b.new_page()
                pg.set_content("<title>ok</title><h1 id=x>lambda-chromium</h1>")
                out = {"selftest": "ok", "text": pg.locator("#x").inner_text(),
                       "title": pg.title(), "chromium": p.chromium.name}
                b.close()
            return out
    except Exception as e:  # never surface a raw exception to the caller
        logger.exception("apply_executor handler fault on %s", method)
        return {"status": "failed", "error": str(e)[:300],
                "confirmation": None, "screenshot_ref": None, "wall": None}
    logger.warning("apply_executor: unknown method %r", method)
    return {"status": "executor_unavailable", "error": f"unknown_method:{method}"}

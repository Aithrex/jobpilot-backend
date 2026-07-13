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
            # Validation-only: drive a CONTROLLED test form (our own hosted
            # replica) end-to-end through the real submit path, exercising the
            # real S3 screenshot sink. NOT for real employer URLs — the caller
            # supplies the target explicitly. resume_b64 = base64 PDF bytes.
            import base64
            url = args.get("url") or ""
            answers = args.get("answers") or {}
            application = args.get("application") or {"application_id": "submit-test"}
            resume = None
            rb = args.get("resume_b64")
            if rb:
                resume = (args.get("resume_name") or "resume.pdf", base64.b64decode(rb))
            return ex._drive_greenhouse(url, answers, resume, application)
        if method == "selftest":
            # Prove Chromium actually boots inside the Lambda sandbox (no network,
            # no employer contacted). A harmless diagnostic kept in prod.
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

"""Workday fixture: create-account + OTP gate helpers."""
from pathlib import Path

import pytest

playwright = pytest.importorskip("playwright")

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "ats"


def _page(html_name: str):
    from playwright.sync_api import sync_playwright
    html = (FIXTURES / html_name).read_text(encoding="utf-8")
    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=True)
    page = browser.new_page()
    page.set_content(html)
    return pw, browser, page


def test_workday_account_fill_signin():
    from services.ats_adapters import WorkdayAdapter, TAKEOVER_ACCOUNT_REQUIRED
    pw, browser, page = _page("workday_account_required.html")
    try:
        ad = WorkdayAdapter(page, "https://aithrex.wd1.myworkdayjobs.com/en-US/careers/job/x_1")
        assert ad._account_gate_present()
        wall = ad._check_takeover()
        assert wall and wall["reason"] == TAKEOVER_ACCOUNT_REQUIRED
        ok = ad.fill_account_credentials("a@b.com", "Passw0rd!Vault", create=False)
        assert ok is True
        assert page.locator("[data-automation-id=email]").input_value() == "a@b.com"
    finally:
        browser.close()
        pw.stop()


def test_workday_otp_automation_ids_detected():
    from services.ats_adapters import WorkdayAdapter, TAKEOVER_MFA
    html = """<!doctype html><html><body>
    <div data-automation-id=otpInput>
      <input data-automation-id=otpCode aria-label="Verification code" type="text">
    </div>
    <button data-automation-id=clickToVerify>Verify</button>
    </body></html>"""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        page = b.new_page()
        page.set_content(html)
        ad = WorkdayAdapter(page, "https://aithrex.wd1.myworkdayjobs.com/job/x_1")
        assert ad._otp_gate_present()
        wall = ad._check_takeover()
        assert wall and wall["reason"] == TAKEOVER_MFA
        b.close()


def test_workday_apply_cta_clears_cookie_banner():
    """Intel-style overlay must not block Apply; CTA click should succeed."""
    from services.ats_adapters import WorkdayAdapter
    html = """<!doctype html><html><body>
    <div id="cookie" style="position:fixed;inset:0;z-index:9999;background:#fff">
      Welcome! uses cookies
      <button>Decline</button>
      <button>Accept Cookies</button>
    </div>
    <h1>Cloud Application Development Engineer</h1>
    <button data-automation-id=jobPostingApplyButton>Apply</button>
    <script>
      document.querySelectorAll('#cookie button').forEach(b =>
        b.addEventListener('click', () => document.getElementById('cookie').remove()));
      document.querySelector('[data-automation-id=jobPostingApplyButton]')
        .addEventListener('click', () => {
          document.body.innerHTML =
            '<input data-automation-id=email type=email>' +
            '<input data-automation-id=password type=password>';
        });
    </script>
    </body></html>"""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        page = b.new_page()
        page.set_content(html)
        ad = WorkdayAdapter(page, "https://intel.wd1.myworkdayjobs.com/job/x_1")
        assert ad.click_apply_cta() is True
        assert page.locator("[data-automation-id=email]").count() == 1
        assert page.locator("#cookie").count() == 0
        b.close()


def test_workday_create_does_not_click_header_sign_in():
    """Create Account submit must not hit the header Sign In control."""
    from services.ats_adapters import WorkdayAdapter
    html = """<!doctype html><html><body>
    <header><button id="hdr">Sign In</button></header>
    <h1>Create Account</h1>
    <input data-automation-id=email type=email>
    <input data-automation-id=password type=password>
    <input data-automation-id=verifyPassword type=password>
    <button data-automation-id=createAccountSubmitButton>Create Account</button>
    <script>
      let opened = false;
      document.getElementById('hdr').onclick = () => { opened = true; window.__opened = true; };
      document.querySelector('[data-automation-id=createAccountSubmitButton]')
        .addEventListener('click', () => { window.__created = true; });
    </script>
    </body></html>"""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        page = b.new_page()
        page.set_content(html)
        ad = WorkdayAdapter(page, "https://intel.wd1.myworkdayjobs.com/apply/applyManually")
        assert ad.fill_account_credentials("a@b.com", "Passw0rd!Vault", create=True)
        assert page.evaluate("window.__created") is True
        assert page.evaluate("window.__opened") is not True
        b.close()


def test_workday_apply_cta_chooses_apply_manually():
    """After Apply, Intel chooser → Apply Manually before account gate."""
    from services.ats_adapters import WorkdayAdapter
    html = """<!doctype html><html><body>
    <button data-automation-id=jobPostingApplyButton>Apply</button>
    <script>
      document.querySelector('[data-automation-id=jobPostingApplyButton]')
        .addEventListener('click', () => {
          document.body.innerHTML =
            '<h2>Start Your Application</h2>' +
            '<button>Autofill with Resume</button>' +
            '<button data-automation-id=applyManually>Apply Manually</button>';
          document.querySelector('[data-automation-id=applyManually]')
            .addEventListener('click', () => {
              document.body.innerHTML =
                '<input data-automation-id=email type=email>' +
                '<input data-automation-id=password type=password>' +
                '<a data-automation-id=createAccountLink>Create Account</a>';
            });
        });
    </script>
    </body></html>"""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        page = b.new_page()
        page.set_content(html)
        ad = WorkdayAdapter(page, "https://intel.wd1.myworkdayjobs.com/job/x_1")
        assert ad.click_apply_cta() is True
        assert page.locator("[data-automation-id=email]").count() == 1
        assert ad._account_gate_present()
        b.close()

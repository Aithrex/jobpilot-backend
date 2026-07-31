"""Regression suite for the four auto-apply repairs of 2026-07-28.

Every case below is pinned to something that was VERIFIED broken against live
production rows for the single real user, not to a hypothetical:

  1. WORKDAY ACCOUNT CREATION (`ats_adapters.WorkdayAdapter`)
     Newer tenants serve a FEDERATED auth panel — "Sign in with Apple / Google /
     LinkedIn / email" — with no email and no password input in the DOM until
     the email route is chosen. `fill_account_credentials` looked straight for
     those inputs, found none and returned False, so the account gate never
     cleared. Observed live on NXP, State Street and Midland States Bank.

  2. OTP GRANT BINDING (`application_otp_service._active_freeze`)
     With eight live freezes (five CAPTCHA, three verify) the grant was bound to
     whichever freeze was touched last — routinely a CAPTCHA row, which no
     emailed code can ever clear, burning the grant.

  3. WALL HAND-OFF (`auto_apply_service`)
     A wall is a HUMAN hand-off by design (we never solve or circumvent a
     CAPTCHA). Exhausting the re-check ladder used to `mark_failed` the row,
     deleting the only actionable thing the user had. It must stay parked and
     actionable, notify the user immediately, and resume on demand.

  4. ENUMERATED-FIELD OPTIONS (`ats_executor`, `browser_executor`)
     Recon dropped the accepted choices, so a select question reached the FE
     with no options, the FE rendered a text box, and nothing the user typed
     could ever match — an unanswerable question that re-parked forever.

Fully offline: mongomock via conftest/_vg_support, no browser, no network, no
LLM, and no live application is ever submitted.
"""
import pytest

from _vg_support import current_db

import services.application_ledger_service as als
import services.auto_apply_service as aas
from services.executor_adapter import (
    ORACLE_READY, SUBMIT_FAILED, SUBMIT_WAITING_VERIFY)

USER = "alice@aspirely.me"

_COLLECTIONS = ("applications", "auto_apply_freezes", "auto_apply_answers",
                "application_otp_grants", "notification_prefs",
                "notification_log", "resume_jobs")


@pytest.fixture()
def db():
    d = current_db()
    for c in _COLLECTIONS:
        d[c].delete_many({})
    yield d
    for c in _COLLECTIONS:
        d[c].delete_many({})


# ===========================================================================
# 1. Workday federated auth panel — reveal the email form before filling
# ===========================================================================
FEDERATED_PANEL = """<!doctype html><html><body>
<div data-automation-id="signInContent">
  <h1>Sign In</h1>
  <p>Create an account or sign in to apply.</p>
  <button data-automation-id="appleSignIn">Sign in with Apple</button>
  <button data-automation-id="googleSignIn">Sign in with Google</button>
  <button data-automation-id="linkedInSignIn">Sign in with LinkedIn</button>
  <button id="email-route">Sign in with email</button>
</div>
</body></html>"""

EMAIL_PANEL = """<!doctype html><html><body>
<div data-automation-id="signInContent">
  <input data-automation-id="email" type="email">
  <input data-automation-id="password" type="password">
  <button data-automation-id="signInSubmitButton" type="button">Sign In</button>
</div>
</body></html>"""


class _RevealPage:
    """The ONE stateful thing this behaviour needs: clicking the "with email"
    control swaps the federated panel for the credential form. Implements only
    the duck-typed surface `_reveal_email_auth_form` touches (the same surface
    `tests/ats_dom.FakePage` models), so no browser is required."""

    def __init__(self, html=FEDERATED_PANEL, reveal_html=EMAIL_PANEL):
        from ats_dom import FakePage
        self._page = FakePage(html)
        self._reveal_html = reveal_html
        self.clicked = []
        self.url = "https://nxp.wd3.myworkdayjobs.com/en-US/careers/job/x/apply/applyManually"

    # -- delegation to the static DOM ------------------------------------
    def query_selector(self, css):
        el = self._page.query_selector(css)
        return _RevealEl(el, self) if el is not None else None

    def query_selector_all(self, css):
        return [_RevealEl(e, self) for e in self._page.query_selector_all(css)]

    def inner_text(self, selector="body"):
        return self._page.inner_text(selector)

    def wait_for_timeout(self, ms):
        pass

    def set_default_timeout(self, ms):
        pass

    def _on_click(self, text):
        self.clicked.append(text)
        if "email" in (text or "").lower() and self._reveal_html:
            from ats_dom import FakePage
            self._page = FakePage(self._reveal_html)
            self._reveal_html = None


class _RevealEl:
    def __init__(self, el, page):
        self._el = el
        self._page = page

    def get_attribute(self, name):
        return self._el.get_attribute(name)

    def inner_text(self):
        return self._el.inner_text()

    def is_visible(self):
        return self._el.is_visible()

    def click(self):
        self._page._on_click(self._el.inner_text())

    def fill(self, value):
        self._el.fill(value)


def _adapter(page):
    from services.ats_adapters import WorkdayAdapter
    return WorkdayAdapter(page, page.url)


class TestWorkdayFederatedAuthPanel:
    def test_credential_inputs_are_absent_until_the_email_route_is_chosen(self):
        """The precondition the old code silently assumed."""
        page = _RevealPage()
        ad = _adapter(page)
        # The gate IS on screen (account copy) but there is nothing to fill.
        assert ad._account_gate_present() is True
        assert ad._credential_inputs_present() is False

    def test_reveal_clicks_the_email_route_and_exposes_the_form(self):
        page = _RevealPage()
        ad = _adapter(page)
        assert ad._reveal_email_auth_form() is True
        assert ad._credential_inputs_present() is True
        assert page.clicked == ["Sign in with email"]

    def test_reveal_never_clicks_an_identity_provider_button(self):
        """Apple / Google / LinkedIn start an OAuth flow we have no account for
        and cannot complete — clicking one strands the run."""
        page = _RevealPage(reveal_html=None)
        ad = _adapter(page)
        ad._reveal_email_auth_form()
        for text in page.clicked:
            assert "apple" not in text.lower()
            assert "google" not in text.lower()
            assert "linkedin" not in text.lower()

    def test_reveal_is_a_noop_when_the_form_is_already_showing(self):
        page = _RevealPage(html=EMAIL_PANEL)
        ad = _adapter(page)
        assert ad._reveal_email_auth_form() is False
        assert page.clicked == []

    def test_reveal_returns_false_when_the_panel_offers_no_email_route(self):
        html = """<!doctype html><html><body>
        <div data-automation-id="signInContent"><h1>Sign in to apply</h1>
        <button>Sign in with Google</button></div></body></html>"""
        page = _RevealPage(html=html, reveal_html=None)
        ad = _adapter(page)
        assert ad._reveal_email_auth_form() is False


def test_email_auth_regex_matches_the_email_route_only():
    from services.ats_adapters import _EMAIL_AUTH_RE
    for yes in ("Sign in with email", "Sign In with Email", "Sign up with email",
                "Continue with email", "Use my email", "Email and password",
                "Log in with an email"):
        assert _EMAIL_AUTH_RE.search(yes), yes
    for no in ("Sign in with Apple", "Sign in with Google",
               "Sign in with LinkedIn", "Email us", "Create Account", "Sign In"):
        assert not _EMAIL_AUTH_RE.search(no), no


# ===========================================================================
# 2. OTP grant binding — an emailed code can never clear a CAPTCHA
# ===========================================================================
class TestOtpGrantBinding:
    def _freeze(self, db, aid, kind, minute):
        from datetime import datetime, timezone
        db.auto_apply_freezes.insert_one({
            "user_email": USER, "application_id": aid, "kind": kind,
            "status": "waiting_verify", "attempts": 0,
            "updated_at": datetime(2026, 7, 28, 5, minute, tzinfo=timezone.utc)})

    def test_grant_binds_to_the_verify_freeze_not_the_newer_captcha(self, db):
        from services import application_otp_service as otp
        self._freeze(db, "app-verify", "verify", 10)
        self._freeze(db, "app-captcha", "captcha", 30)   # newer

        res = otp.capture_from_noise_message(
            user_email=USER, message_id="<m1@workday.com>",
            subject="Your verification code",
            text="Your verification code is 481920", html="",
            from_addr="noreply@workday.com")

        assert res["captured"] is True
        assert res["application_id"] == "app-verify"

    def test_grant_stays_unbound_when_only_captcha_walls_are_open(self, db):
        """Better claimable-by-anyone than burned on a wall no email clears."""
        from services import application_otp_service as otp
        self._freeze(db, "app-captcha", "captcha", 30)

        res = otp.capture_from_noise_message(
            user_email=USER, message_id="<m2@workday.com>",
            subject="Your verification code",
            text="Your verification code is 481920", html="",
            from_addr="noreply@workday.com")

        assert res["captured"] is True
        assert res["application_id"] is None
        # …and a LATER verify wall can still claim it.
        claimed = otp.claim_for_verify(USER, "app-verify-later")
        assert claimed and claimed["code"] == "481920"


# ===========================================================================
# 3. Wall hand-off — stays parked, notifies, and resumes on demand
# ===========================================================================
class _WallExecutor:
    """Submits straight into a CAPTCHA wall; its `verify_state` answer is
    scripted per call so a test can say "the human has now cleared it"."""

    def __init__(self, *results):
        self._results = list(results)
        self.calls = 0

    def is_available(self):
        return True

    def recon_form(self, apply_url, ats_family):
        return {"questions": [], "needs_account": False}

    def submit(self, application, answers, resume_pdf):
        return {"status": SUBMIT_WAITING_VERIFY, "confirmation": None,
                "screenshot_ref": None,
                "wall": {"kind": "captcha", "needs": ["captcha"]}, "error": None}

    def create_account(self, *a, **kw):
        return {"status": "executor_unavailable"}

    def verify_state(self, application, freeze):
        self.calls += 1
        if self._results:
            return self._results.pop(0)
        return {"status": SUBMIT_WAITING_VERIFY, "resolved": False,
                "wall": {"kind": "captcha", "needs": ["captcha"]}}


def _parked_row(user=USER, *, apply_url="https://job-boards.greenhouse.io/a/jobs/1",
                executor=None):
    """A row driven through the REAL path into a parked CAPTCHA wall: created →
    gathering → ready_for_review → approved → submit() hits the wall →
    `_park_verify` lands it in needs_input."""
    view, _ = als.create_application(
        user, company="Acme", title="Cloud Engineer",
        jd_text="Build AWS infra with Terraform and Kubernetes.",
        source="greenhouse", ats_family="greenhouse", apply_url=apply_url)
    aid = view["application_id"]
    als.mark_gathering(user, aid)
    als.mark_ready_for_review(user, aid, autonomy="auto", confidence_score=0.95)
    als.approve(user, [aid], approved_by="system")
    aas.set_executor(executor or _WallExecutor())
    try:
        res = aas.submit(aid, user)
    finally:
        aas.set_executor(None)
    assert res["status"] == "waiting_verify", res
    return aid


class TestWallPromptCopy:
    def test_captcha_copy_sends_the_user_to_the_employer_site(self):
        prompt = aas.wall_prompt("captcha", ["captcha"])
        assert "employer's site" in prompt
        # It is a hand-off, NOT a question we are waiting on an answer for.
        assert "needs your answer" not in prompt.lower()

    def test_an_account_gate_is_not_described_as_an_email_verification(self):
        """The executor reports an ACCOUNT wall as kind='verify' (there is no
        dedicated constant), and telling the user to check their email sends
        them hunting for a mail that will never arrive."""
        prompt = aas.wall_prompt("verify", ["account_required"])
        assert "account" in prompt
        assert "email confirmation" not in prompt

    def test_plain_verify_still_reads_as_an_email_confirmation(self):
        assert "email confirmation" in aas.wall_prompt("verify", ["email_verification"])


@pytest.fixture()
def notified(monkeypatch):
    """Capture wall notifications instead of dispatching them."""
    sent = []
    monkeypatch.setattr(
        "services.notifications.service.send",
        lambda email, event, payload=None: sent.append((email, event, payload)))
    return sent


class TestWallParksAndNotifies:
    def test_parking_notifies_the_user_with_a_deep_link_once(self, db, notified):
        aid = _parked_row()
        assert len(notified) == 1
        email, event, payload = notified[0]
        assert email == USER
        assert event == aas.WALL_NOTIFY_EVENT
        assert payload["application_id"] == aid
        assert aid in payload["cta_url"]
        assert "/app/apply/auto-apply" in payload["cta_url"]

        # A re-park of the SAME freeze must not notify again (the executor
        # re-drives a row several times before the ladder is exhausted).
        aas._park_verify(USER, aid, {"kind": "captcha", "needs": ["captcha"]})
        assert len(notified) == 1

    def test_a_repark_refreshes_the_wall_item_of_an_already_parked_row(
            self, db, notified):
        """needs_input -> needs_input is not a legal TRANSITION, but the wall can
        change between runs (an account gate that becomes an email
        verification). Dropping the update would leave the user staring at copy
        for a wall that is no longer the one blocking them."""
        aid = _parked_row()
        assert als.get_application(USER, aid)["needs_input"][0]["wall_kind"] == "captcha"

        aas._park_verify(USER, aid,
                         {"kind": "verify", "needs": ["account_required"]})
        item = als.get_application(USER, aid)["needs_input"][0]
        assert item["wall_kind"] == "verify"
        assert item["kind"] == "oracle_verify"
        assert "account" in item["question"]

    def test_a_notification_failure_never_breaks_the_park(self, db, monkeypatch):
        def _boom(*a, **kw):
            raise RuntimeError("SES down")

        monkeypatch.setattr("services.notifications.service.send", _boom)
        aid = _parked_row()
        assert als.get_application(USER, aid)["status"] == als.STATUS_NEEDS_INPUT

    def test_the_parked_item_carries_the_wall_kind_and_deep_link(self, db, notified):
        aid = _parked_row()
        item = als.get_application(USER, aid)["needs_input"][0]
        assert item["kind"] == "oracle_captcha"
        assert item["wall_kind"] == "captcha"
        assert item["action"] == "complete_in_browser"
        assert item["apply_url"] == "https://job-boards.greenhouse.io/a/jobs/1"
        assert "employer's site" in item["question"]


class TestExhaustedWallStaysActionable:
    def test_exhausting_the_ladder_does_not_fail_the_row(self, db, notified):
        """A CAPTCHA we correctly refuse to solve must not become a failure."""
        aid = _parked_row()
        aas.set_executor(_WallExecutor())
        try:
            for _ in range(aas._VERIFY_MAX_ATTEMPTS):
                out = aas.recheck_verify(aid, USER)
        finally:
            aas.set_executor(None)

        assert out["status"] == aas.FREEZE_AWAITING_HUMAN
        freeze = db.auto_apply_freezes.find_one({"application_id": aid})
        assert freeze["status"] == aas.FREEZE_AWAITING_HUMAN
        # The row is STILL the actionable hand-off card, not a failure.
        row = als.get_application(USER, aid)
        assert row["status"] == als.STATUS_NEEDS_INPUT
        assert row["needs_input"][0]["kind"] == "oracle_captcha"

    def test_an_exhausted_freeze_is_no_longer_swept(self, db, notified):
        """Stop burning executor invocations on a wall only a human can clear."""
        aid = _parked_row()
        aas.set_executor(_WallExecutor())
        try:
            for _ in range(aas._VERIFY_MAX_ATTEMPTS):
                aas.recheck_verify(aid, USER)
        finally:
            aas.set_executor(None)
        assert [d["application_id"] for d in aas.due_verify_waits()] == []


class TestResumeAfterWall:
    def test_user_cleared_the_wall_revives_and_rechecks_immediately(
            self, db, notified, monkeypatch):
        enqueued = []
        monkeypatch.setattr(aas, "_enqueue_submit",
                            lambda aid, email: enqueued.append(aid) or "job-1")
        ex = _WallExecutor()
        aid = _parked_row(executor=ex)
        aas.set_executor(ex)
        try:
            for _ in range(aas._VERIFY_MAX_ATTEMPTS):
                aas.recheck_verify(aid, USER)
            assert db.auto_apply_freezes.find_one(
                {"application_id": aid})["status"] == aas.FREEZE_AWAITING_HUMAN

            # The human finished it on the employer's site.
            ex._results = [{"status": ORACLE_READY, "resolved": True,
                            "wall": None, "error": None}]
            out = aas.resume_after_wall(USER, aid)
        finally:
            aas.set_executor(None)

        assert out["status"] == "resumed"
        assert out["recheck"] == "thawed"
        # Thawed on the normal approved+snapshot path, submit re-enqueued async.
        assert enqueued == [aid]
        assert db.auto_apply_freezes.find_one(
            {"application_id": aid})["status"] == "resolved"

    def test_resume_resets_the_backoff_ladder(self, db, notified):
        aid = _parked_row()
        aas.set_executor(_WallExecutor())
        try:
            aas.recheck_verify(aid, USER)                 # attempts -> 1
            assert db.auto_apply_freezes.find_one(
                {"application_id": aid})["attempts"] == 1
            aas.resume_after_wall(USER, aid)              # user cleared it
        finally:
            aas.set_executor(None)
        freeze = db.auto_apply_freezes.find_one({"application_id": aid})
        # One inline re-check happened, so the ladder restarted from 0 and
        # bumped once — NOT from 1.
        assert freeze["attempts"] == 1
        assert freeze["human_cleared_at"] is not None

    def test_resume_on_a_row_with_no_wall_is_409_not_a_silent_noop(self, db):
        view, _ = als.create_application(
            USER, company="Acme", title="Cloud Engineer", jd_text="Build infra.",
            source="greenhouse", ats_family="greenhouse",
            apply_url="https://job-boards.greenhouse.io/a/jobs/2")
        with pytest.raises(aas.NoWallToResume):
            aas.resume_after_wall(USER, view["application_id"])

    def test_resume_is_owner_scoped(self, db, notified):
        aid = _parked_row()
        with pytest.raises(als.ApplicationNotFound):
            aas.resume_after_wall("mallory@evil.test", aid)


# ===========================================================================
# 4. Enumerated-field options survive recon
# ===========================================================================
_GH_SELECT_PAYLOAD = {
    "questions": [
        {"required": True,
         "label": ("We value in-person collaboration, and this role requires an "
                   "in-office presence. Are you able to work from our office?"),
         "fields": [{"name": "question_18077811008",
                     "type": "multi_value_single_select",
                     "values": [{"label": "Yes, 5 days a week", "value": 1},
                                {"label": "Yes, 3 days a week", "value": 2},
                                {"label": "No", "value": 3}]}]},
        {"required": True, "label": "What are your base salary expectations?",
         "fields": [{"name": "desired_salary", "type": "input_text"}]},
    ],
}


class _GhResp:
    status_code = 200

    def json(self):
        return _GH_SELECT_PAYLOAD


class _GhSession:
    def __init__(self):
        self.headers = {}

    def get(self, url, timeout=None):
        return _GhResp()


class TestReconCarriesOptions:
    def test_greenhouse_recon_carries_option_labels(self):
        from services.ats_executor import AtsExecutor
        res = AtsExecutor(session=_GhSession()).recon_form(
            "https://boards.greenhouse.io/k2space/jobs/42", "greenhouse")
        by_id = {q["id"]: q for q in res["questions"]}
        assert by_id["question_18077811008"]["options"] == [
            "Yes, 5 days a week", "Yes, 3 days a week", "No"]
        # A free-text field carries no options at all (not an empty list).
        assert "options" not in by_id["desired_salary"]

    def test_options_reach_the_needs_input_item_the_frontend_renders(self):
        """End of the chain: recon -> normalize -> needs item. Without this the
        FE has nothing to render, falls back to a text box, and the typed answer
        can never match a real option (`no_option_match` -> re-park forever)."""
        from services.ats_executor import AtsExecutor
        from services import apply_forms
        res = AtsExecutor(session=_GhSession()).recon_form(
            "https://boards.greenhouse.io/k2space/jobs/42", "greenhouse")
        normalized = apply_forms.normalize_recon_questions(res["questions"])
        select_q = next(q for q in normalized
                        if q["recon_id"] == "question_18077811008")
        assert select_q["options"] == [
            "Yes, 5 days a week", "Yes, 3 days a week", "No"]

        item = aas._needs_item(select_q, kind="screening")
        assert item["options"] == [
            "Yes, 5 days a week", "Yes, 3 days a week", "No"]
        assert item["value_type"] == "select"

    def test_option_labels_are_deduped_capped_and_shape_tolerant(self):
        from services.ats_executor import _option_labels
        assert _option_labels([{"label": "Yes"}, {"label": "Yes"},
                               {"label": "No"}]) == ["Yes", "No"]
        # A bare string or a label-less entry degrades, never raises.
        assert _option_labels(["Maybe", {"value": "Other"}]) == ["Maybe", "Other"]
        assert _option_labels(None) == []
        assert _option_labels("not a list") == []
        assert len(_option_labels([{"label": "o%d" % i} for i in range(80)])) == 40

    def test_browser_recon_threads_options_into_the_question(self):
        """Lever/Ashby path: whatever `_FORM_RECON_JS` captured must survive the
        shaping step (this is exactly where the labels used to be dropped)."""
        from services.browser_executor import BrowserExecutor
        out = BrowserExecutor._shape_recon_fields(
            [{"name": "howHeard", "type": "select", "label": "How did you hear?",
              "required": True, "options": ["LinkedIn", "Referral"]},
             {"name": "note", "type": "textarea", "label": "Anything else?",
              "required": False},
             {"name": "resume", "type": "file", "label": "Resume"}],
            {"resume"}, ())
        by_id = {q["id"]: q for q in out}
        assert by_id["howHeard"]["options"] == ["LinkedIn", "Referral"]
        assert "options" not in by_id["note"]
        assert "resume" not in by_id          # executor-supplied, still dropped

    def test_the_recon_js_captures_options_for_select_and_radio_groups(self):
        """The capture itself is browser-side JS, so pin the CONTRACT: the
        extractor must read <option> text and same-named radio labels, and must
        not emit the placeholder row."""
        from services.browser_executor import _FORM_RECON_JS
        assert "optionsFor" in _FORM_RECON_JS
        assert "el.options" in _FORM_RECON_JS          # <select>
        assert "input[type=" in _FORM_RECON_JS         # radio / checkbox group
        assert "entry.options = opts" in _FORM_RECON_JS
        # Placeholder rows ("Select…", "Please choose one") are dropped, but the
        # pattern is anchored at both ends so a real option that merely starts
        # with the word survives.
        assert "please\\s+" in _FORM_RECON_JS
        assert "(select|choose)\\b" in _FORM_RECON_JS


# ===========================================================================
# 1b. The account gate must fall back to CREATE, whatever the URL says
# ===========================================================================
class _GatePage:
    """Minimal Playwright-shaped page for `_workday_clear_account_gate`.

    Only what that method touches: `url`, `inner_text`, `title`, the timing
    no-ops, and a `locator()` that reports "nothing here" so the method's own
    branches — not a fixture's DOM — decide what happens."""

    def __init__(self, url):
        self.url = url
        self.body = "Sign In  Sign in with Apple  Sign in with Google  Sign in with email"

    def inner_text(self, selector="body"):
        return self.body

    def title(self):
        return "Sign In"

    def wait_for_timeout(self, ms):
        pass

    def wait_for_load_state(self, state, timeout=None):
        pass

    def screenshot(self, **kw):
        return b"png"

    def locator(self, selector):
        return _EmptyLocator()


class _EmptyLocator:
    def __init__(self):
        self.first = self

    def count(self):
        return 0

    def is_visible(self):
        return False

    def click(self, **kw):
        raise AssertionError("nothing to click")


class _GateAdapter:
    """Records every fill_account_credentials call; the gate never clears."""

    def __init__(self):
        self.calls = []

    def fill_account_credentials(self, username, password, *, create=False):
        self.calls.append("create" if create else "signin")
        return False

    def _account_gate_present(self):
        return True

    def _otp_gate_present(self):
        return False

    def _qs(self, css):
        return None

    def _qsa(self, css):
        return []

    def dismiss_overlays(self):
        pass


def _gate_executor(monkeypatch, ex):
    # The account-gate flow now PREFLIGHTS the managed mailbox: registering a
    # candidate account commits us to reading the employer's verification
    # email, so it refuses to start when this deployment cannot receive mail.
    # These tests are about what happens once that precondition holds, so state
    # it explicitly rather than depending on ambient config.
    from services import managed_mailbox_service as mailbox
    monkeypatch.setattr(mailbox, "can_receive_mail", lambda: True)
    monkeypatch.setattr(type(ex), "_managed_apply_email",
                        lambda self, email: "alias@my-private-mail.com")
    monkeypatch.setattr(type(ex), "create_account",
                        lambda self, *a, **kw: {"resolved": True, "_password": "s3cret!",
                                                "username": "alias@my-private-mail.com"})
    monkeypatch.setattr(type(ex), "_shoot", lambda self, page, app: None)


class TestWorkdayAccountCreateFallback:
    APPLY_MANUALLY = ("https://statestreet.wd1.myworkdayjobs.com/en-US/Global/"
                      "job/Krakow/Engineer_R-1/apply/applyManually")

    def test_create_is_attempted_on_an_apply_manually_url(self, monkeypatch):
        """The live regression: the fallback used to require "login" in the URL,
        but the wall is served at /apply/applyManually — so Create Account was
        NEVER reached and no employer account was ever registered."""
        from services.browser_executor import BrowserExecutor
        ex = BrowserExecutor()
        _gate_executor(monkeypatch, ex)
        adapter = _GateAdapter()
        cleared = ex._workday_clear_account_gate(
            _GatePage(self.APPLY_MANUALLY), adapter,
            {"apply_url": self.APPLY_MANUALLY, "company": "State Street"},
            "alice@aspirely.me", "statestreet")

        assert cleared is False           # this adapter never clears — that is fine
        assert "signin" in adapter.calls  # sign-in tried first (account may exist)
        assert "create" in adapter.calls  # …and CREATE was reached. This is the fix.
        assert adapter.calls.index("signin") < adapter.calls.index("create")

    def test_a_vault_miss_still_short_circuits_without_touching_the_form(
            self, monkeypatch):
        from services.browser_executor import BrowserExecutor
        ex = BrowserExecutor()
        monkeypatch.setattr(BrowserExecutor, "_managed_apply_email",
                            lambda self, email: "alias@my-private-mail.com")
        monkeypatch.setattr(BrowserExecutor, "create_account",
                            lambda self, *a, **kw: {"resolved": False,
                                                    "error": "vault_down"})
        adapter = _GateAdapter()
        assert ex._workday_clear_account_gate(
            _GatePage(self.APPLY_MANUALLY), adapter,
            {"apply_url": self.APPLY_MANUALLY}, "alice@aspirely.me",
            "statestreet") is False
        assert adapter.calls == []


# ===========================================================================
# 3b. The route the FE calls when the user comes back from the employer tab
# ===========================================================================
@pytest.fixture()
def jobs_app(monkeypatch):
    from _vg_support import load_service_app
    monkeypatch.setenv("AUTO_SUBMIT_ENABLED", "true")
    return load_service_app("jobs")


def _call(flask_app, path, body, identity=USER):
    from flask_jwt_extended import create_access_token
    with flask_app.app_context():
        tok = create_access_token(identity=identity)
    return flask_app.test_client().post(
        path, headers={"Authorization": f"Bearer {tok}"}, json=body)


class TestWallClearedRoute:
    PATH = "/api/apply/needs-input/wall-cleared"

    def test_a_cleared_wall_resumes_the_row(self, db, jobs_app, notified,
                                            monkeypatch):
        monkeypatch.setattr(aas, "_enqueue_submit", lambda aid, email: "job-1")
        ex = _WallExecutor()
        aid = _parked_row(executor=ex)
        ex._results = [{"status": ORACLE_READY, "resolved": True,
                        "wall": None, "error": None}]
        aas.set_executor(ex)
        try:
            resp = _call(jobs_app, self.PATH, {"application_id": aid})
        finally:
            aas.set_executor(None)
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["status"] == "resumed"
        assert body["recheck"] == "thawed"

    def test_a_row_with_no_wall_is_409_never_401(self, db, jobs_app):
        view, _ = als.create_application(
            USER, company="Acme", title="Eng", jd_text="Build infra.",
            source="greenhouse", ats_family="greenhouse",
            apply_url="https://job-boards.greenhouse.io/a/jobs/3")
        resp = _call(jobs_app, self.PATH,
                     {"application_id": view["application_id"]})
        assert resp.status_code == 409
        assert resp.get_json()["error"] == "no_wall_to_resume"

    def test_a_missing_application_id_is_400(self, db, jobs_app):
        resp = _call(jobs_app, self.PATH, {})
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "invalid_request"

    def test_another_users_application_is_404_never_a_cross_tenant_resume(
            self, db, jobs_app, notified):
        aid = _parked_row()
        resp = _call(jobs_app, self.PATH, {"application_id": aid},
                     identity="mallory@evil.test")
        assert resp.status_code == 404

    def test_no_token_is_401_and_only_that(self, db, jobs_app):
        """The ONE place 401 is correct: a missing bearer token."""
        resp = jobs_app.test_client().post(self.PATH, json={"application_id": "x"})
        assert resp.status_code == 401


class TestWorkdayCredentialFill:
    def test_fill_account_credentials_reveals_the_form_before_looking_for_it(self):
        """The wiring, not just the helper: `fill_account_credentials` must run
        the reveal FIRST. (The fill itself then needs Playwright `locator`
        selectors this offline page does not model, so it returns False — but the
        credential inputs are now in the DOM, which is the whole point.)"""
        page = _RevealPage()
        ad = _adapter(page)
        assert ad._credential_inputs_present() is False
        ad.fill_account_credentials("alias@my-private-mail.com", "s3cret!",
                                    create=False)
        assert ad._credential_inputs_present() is True
        assert page.clicked[0] == "Sign in with email"


# ===========================================================================
# 5. A résumé we failed to attach is not a question
# ===========================================================================
class _FieldFailExecutor(_WallExecutor):
    def __init__(self, field, reason="fill_failed"):
        super().__init__()
        self._field = field
        self._reason = reason

    def submit(self, application, answers, resume_pdf):
        return {"status": SUBMIT_FAILED, "confirmation": None,
                "screenshot_ref": None, "wall": None, "error": "needs_input",
                "field": self._field, "reason": self._reason}


def _submit_with(executor, user=USER):
    view, _ = als.create_application(
        user, company="Supabase", title="Engineer", jd_text="Build Postgres infra.",
        source="greenhouse", ats_family="greenhouse",
        apply_url="https://job-boards.greenhouse.io/supabase/jobs/7")
    aid = view["application_id"]
    als.mark_gathering(user, aid)
    als.mark_ready_for_review(user, aid, autonomy="auto", confidence_score=0.95)
    als.approve(user, [aid], approved_by="system")
    aas.set_executor(executor)
    try:
        aas.submit(aid, user)
    finally:
        aas.set_executor(None)
    return aid


class TestResumeGapIsNotAQuestion:
    def test_a_failed_resume_upload_reads_as_an_attachment_problem(self, db):
        """Live (Supabase): the résumé gap surfaced as a `screening` item saying
        "We need your answer to continue (resume)." — a text box the user could
        type into but could never satisfy."""
        aid = _submit_with(_FieldFailExecutor("resume"))
        item = als.get_application(USER, aid)["needs_input"][0]
        assert item["kind"] == "attachment_failed"
        assert "We need your answer to continue" not in item["question"]
        assert "attach your résumé" in item["question"]
        assert item["action"] == "complete_in_browser"
        # And it is NOT answerable, so the answer loop cannot pretend to clear it.
        assert aas._answerable(item) is False

    def test_a_real_screening_field_is_untouched(self, db):
        aid = _submit_with(_FieldFailExecutor("desired_salary", "unresolved"))
        item = als.get_application(USER, aid)["needs_input"][0]
        assert item["kind"] == "screening"
        assert aas._answerable(item) is True

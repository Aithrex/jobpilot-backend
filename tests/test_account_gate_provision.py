"""Employer account gate (spec §4): WE create the account, the user does not.

The bug this pins: recon reported ``needs_account`` and gather turned that
straight into ``has_gaps`` → ``needs_input``. ``create_account`` lives ONLY
inside the submit drive (``browser_executor._workday_clear_account_gate``), so
a row parked on the account wall never reached it — and the wall is not
answerable, so the row could never leave ``needs_input`` either. The user saw
"Employer account required - sign in or create one on their career site" and
nothing ever happened.

Runs fully offline (mongomock, no browser, no AWS).
"""
import pytest

from _vg_support import current_db

import services.auto_apply_service as aas
import services.application_ledger_service as ledger
import services.ats_credential_service as creds
from services.executor_adapter import StubExecutor

USER = "gate.user@aspirely.me"
ALIAS = "gate.user@my-private-mail.com"
WORKDAY_URL = "https://acme.wd1.myworkdayjobs.com/en-US/careers/job/Engineer"


class _AccountGateExecutor(StubExecutor):
    """Recon finds no questions but DOES find a login wall — the exact Workday
    shape that produced the dead-end."""
    name = "account-gate"

    def recon_form(self, apply_url, ats_family):
        return {"questions": [], "needs_account": True}


class _FakeCipher:
    def encrypt(self, plaintext, context=None):
        return {"v": 1, "ct": plaintext, "ctx": dict(context or {})}

    def decrypt(self, blob, context=None):
        if blob.get("ctx") != dict(context or {}):
            from services.kms_cipher import KmsCipherError
            raise KmsCipherError("context mismatch")
        return blob["ct"]


@pytest.fixture()
def gate_db(monkeypatch):
    d = current_db()
    for c in ("applications", "auto_apply_answers", "auto_submit_audit",
              "auto_submit_settings", "ats_credentials", "ats_credential_audit",
              "managed_mailboxes", "mail_aliases", "user_resumes",
              "resume_assets", "users", "user_preferences",
              "application_answer_library"):
        d[c].delete_many({})
    monkeypatch.setattr(creds, "_cipher", _FakeCipher())
    monkeypatch.setattr(creds, "_indexes_ensured", False)
    aas.set_executor(_AccountGateExecutor())
    yield d
    aas._executor = None


@pytest.fixture()
def with_alias(monkeypatch):
    from services import managed_mailbox_service as mailbox
    monkeypatch.setattr(mailbox, "apply_alias", lambda *a, **kw: ALIAS)


def _make_app(ats_family="workday", apply_url=WORKDAY_URL):
    view, _outcome = ledger.create_application(
        USER, company="Acme Corp", title="Engineer", jd_text="Build things.",
        source="test", apply_url=apply_url, ats_family=ats_family)
    return view["application_id"]


# ---------------------------------------------------------------------------
# The fix: Workday gate is self-served
# ---------------------------------------------------------------------------
class TestSelfServedAccount:
    def test_gather_provisions_the_login_and_does_not_park(self, gate_db, with_alias):
        aid = _make_app()
        out = aas.gather(aid, USER)

        assert out["account_provisioned"] is True
        assert out["needs_account"] is False
        assert out["status"] != ledger.STATUS_NEEDS_INPUT

        # The vault row the SUBMIT drive will read back exists, under the tenant
        # browser_executor derives from the same URL.
        row = gate_db.ats_credentials.find_one(
            {"user_email": USER, "ats_family": "workday", "tenant": "acme"})
        assert row is not None
        assert row["username"] == ALIAS
        assert row["company"] == "Acme Corp"
        assert creds.reveal(USER, "workday", "acme")["password"]

    def test_the_answer_sheet_no_longer_says_needs_account(self, gate_db, with_alias):
        aid = _make_app()
        aas.gather(aid, USER)
        sheet = gate_db.auto_apply_answers.find_one(
            {"application_id": aid, "user_email": USER})
        assert sheet["needs_account"] is False

    def test_the_executor_tenant_matches_gathers_tenant(self, gate_db, with_alias):
        """browser_executor derives the tenant from the same URL; if these two
        ever diverge the submit drive mints a SECOND account."""
        from services.browser_executor import BrowserExecutor
        assert (aas._workday_tenant_from_url(WORKDAY_URL)
                == BrowserExecutor._workday_tenant_from_url(None, WORKDAY_URL))


# ---------------------------------------------------------------------------
# When we genuinely cannot help, say so — don't pretend
# ---------------------------------------------------------------------------
class TestHonestFallbacks:
    def test_unsupported_vendor_parks_with_an_honest_message(self, gate_db, with_alias):
        aid = _make_app(ats_family="greenhouse",
                        apply_url="https://boards.greenhouse.io/acme/jobs/1")
        out = aas.gather(aid, USER)

        assert out["account_provisioned"] is False
        assert out["account_gate_reason"] == "unsupported_family"
        assert out["status"] == ledger.STATUS_NEEDS_INPUT

        row = ledger.get_application(USER, aid)
        item = [i for i in row["needs_input"] if i.get("kind") == "account"][0]
        assert item["action"] == "complete_in_browser"
        assert "can't create one there yet" in item["question"]
        # and no credential was minted for a vendor we cannot register with.
        assert gate_db.ats_credentials.count_documents({"user_email": USER}) == 0

    def test_no_managed_alias_parks_instead_of_using_the_real_email(
            self, gate_db, monkeypatch):
        from services import managed_mailbox_service as mailbox
        monkeypatch.setattr(mailbox, "apply_alias", lambda *a, **kw: None)

        aid = _make_app()
        out = aas.gather(aid, USER)

        assert out["account_provisioned"] is False
        assert out["account_gate_reason"] == "no_managed_alias"
        assert out["status"] == ledger.STATUS_NEEDS_INPUT
        # The user's REAL address is never registered with the employer.
        assert gate_db.ats_credentials.count_documents({"user_email": USER}) == 0

    def test_kill_switch_restores_the_old_parking_behaviour(
            self, gate_db, with_alias, monkeypatch):
        monkeypatch.setattr(aas, "_auto_account_enabled", lambda: False)
        aid = _make_app()
        out = aas.gather(aid, USER)
        assert out["account_provisioned"] is False
        assert out["account_gate_reason"] == "disabled"
        assert out["status"] == ledger.STATUS_NEEDS_INPUT


# ---------------------------------------------------------------------------
# Copy contract
# ---------------------------------------------------------------------------
class TestAccountGateItem:
    def test_provisioned_copy_says_we_are_doing_it(self):
        item = aas.account_gate_item("workday", provisioned=True,
                                     username=ALIAS, shared=True)
        assert item["kind"] == "account"
        assert item["field_id"] == "ats_account"
        assert "action" not in item          # nothing for the user to do
        assert "We're creating your employer account" in item["question"]
        assert ALIAS in item["question"]

    def test_supported_but_failed_copy_promises_a_retry(self):
        item = aas.account_gate_item("workday", apply_url=WORKDAY_URL)
        assert item["action"] == "complete_in_browser"
        assert item["apply_url"] == WORKDAY_URL
        assert "we'll retry" in item["question"]

    def test_unsupported_copy_names_the_vendor(self):
        item = aas.account_gate_item("ashby")
        assert "ashby" in item["question"]
        assert "can't create one there yet" in item["question"]

    def test_item_survives_the_ledger_sanitizer(self):
        """`_sanitize_needs_input` drops unknown keys — the message and the
        hand-off fields must be ones it keeps."""
        item = aas.account_gate_item("greenhouse", apply_url=WORKDAY_URL)
        kept = ledger._sanitize_needs_input([item])[0]
        assert kept["question"] == item["question"]
        assert kept["kind"] == "account"
        assert kept["field_id"] == "ats_account"
        assert kept["apply_url"] == WORKDAY_URL
        assert kept["action"] == "complete_in_browser"

"""Offline regression suite for THE submit blocker: no résumé was ever attached.

WHAT HAPPENED
-------------
``auto_apply_service._resume_ref`` only ever resolved the TAILORED variant::

    trid = app.get("tailoring_record_id")
    if not trid:
        return None

Tailoring is opt-out (``auto_submit_settings.use_tailored_resume=False`` makes
``gather`` skip the whole prepare/tailor phase — see auto_apply_service.py:1018),
so for such a user EVERY ledger row carries ``tailoring_record_id: None``,
``_resume_ref`` returned None, and the executor was handed
``resume_pdf=None``. Five real applications came back "Missing entry for
required field: Resume" — while the user's parsed base résumé sat in
``resume_assets`` with ``users.active_resume_id`` pointing straight at it.

WHAT IS PINNED HERE
-------------------
1. Untailored row + an active base asset → a BASE reference (ids, never bytes),
   and ``submit`` hands that reference to the executor.
2. The tailored variant STILL WINS when one is linked (fallback, not replacement).
3. A user with NO résumé at all resolves to nothing and the drive PARKS on
   ``resume_missing`` — a CV-less application is never submitted.
4. The S3 lookup is OWNER-SCOPED on both sides: another user's asset id must not
   resolve, and an id with no owner must not resolve at all.
5. The remote-fleet path (``pinned_resume_s3_key`` → /executor/resume-url and
   ``execution_envelope.resume_available``) falls back identically.

Fully offline: mongomock via conftest/_vg_support, the lxml ``tests/ats_dom``
harness for the DOM, a fake storage service. Zero browser, network or AWS.
"""
import pathlib

import pytest

from _vg_support import current_db

from ats_dom import FakePage
import services.application_ledger_service as ledger
import services.auto_apply_service as aas
import services.browser_executor as be
import services.resume_access as resume_access
import services.s3_service as s3
from services.ats_adapters import reset_circuit_breakers
from services.browser_executor import BrowserExecutor
from services.executor_adapter import StubExecutor, SUBMIT_SUCCESS, SUBMIT_FAILED

USER = "harshith@aspirely.me"
OTHER = "mallory@aspirely.me"
_JD = "We are hiring a cloud engineer to run Kubernetes in production."

_FIX = pathlib.Path(__file__).parent / "fixtures" / "ats"
_ASHBY_URL = "https://jobs.ashbyhq.com/aithrex/1234abcd-5678-90ef-1234/application"
_GH_URL = "https://boards.greenhouse.io/aithrex/jobs/4200"
_APP = {"application_id": "app1", "apply_url": _ASHBY_URL, "ats_family": "ashby"}

_CANONICAL_ANSWERS = {
    "full_name": "Harshith Manne",
    "email": "harshith@aspirely.me",
    "first_name": "Harshith",
    "last_name": "Manne",
    "phone": "5551234567",
    "gender": "Male",
    "requires_sponsorship": "Yes",
}

_ASSET_BYTES = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF"

_COLLECTIONS = (
    "applications", "auto_submit_audit", "auto_apply_answers",
    "auto_apply_freezes", "tailoring_records", "resume_assets", "users",
)


@pytest.fixture()
def db():
    d = current_db()
    for c in _COLLECTIONS:
        d[c].delete_many({})
    d.users.insert_one({"email": USER})
    d.users.insert_one({"email": OTHER})
    return d


@pytest.fixture(autouse=True)
def _reset():
    reset_circuit_breakers()
    yield
    reset_circuit_breakers()
    aas.set_executor(None)


class _FakeStorage:
    """Stand-in for s3_service: records the key it was asked for and hands back
    the asset bytes, so the owner-scoping assertions are about the LOOKUP."""

    def __init__(self):
        self.keys = []

    def get_resume(self, s3_key):
        self.keys.append(s3_key)
        return _ASSET_BYTES


@pytest.fixture()
def storage(monkeypatch):
    fake = _FakeStorage()
    monkeypatch.setattr(s3, "get_storage_service", lambda: fake)
    return fake


def _upload_base_resume(user_email, filename="Harshith_Manne_Cloud_Engineer.pdf",
                        mime_type="application/pdf"):
    """The real upload write-order (asset doc → object_key → active pointer)."""
    rid = resume_access.create_pending_asset(
        owner_type="user", user_email=user_email, filename=filename,
        mime_type=mime_type)
    resume_access.attach_object_key(rid, resume_access.asset_object_key(rid))
    current_db().resume_assets.update_one(
        {"resume_id": rid}, {"$set": {"parse_status": "parsed"}})
    resume_access.adopt_active_if_unset(user_email, rid)
    return rid


def _create(company="Acme", title="Cloud Engineer"):
    view, _ = ledger.create_application(
        USER, company=company, title=title, jd_text=_JD, source="greenhouse",
        ats_family="greenhouse", apply_url=_GH_URL)
    return view["application_id"]


def _seat_approved(aid):
    """Drive a queued row to `approved` the way gather+manual approval does —
    with NO tailoring_record_id (the untailored production shape)."""
    ledger.mark_gathering(USER, aid)
    aas._persist_answers(aid, USER, {"email": USER}, 1.0, False,
                         autonomy="ask", confidence_score=0.4,
                         recon_fingerprint="recon-1")
    ledger.mark_ready_for_review(USER, aid, autonomy="ask", confidence_score=0.4)
    approved = ledger.approve(
        USER, [aid], approved_by="user",
        snapshot_provider=lambda a: aas.build_approval_snapshot(USER, a))
    assert approved == 1
    return aid


def _page(name="ashby_form.html", url=_ASHBY_URL):
    return FakePage((_FIX / name).read_text(encoding="utf-8"), url=url,
                    next_html=(_FIX / "confirmation.html").read_text(encoding="utf-8"))


class _SpyExecutor(StubExecutor):
    name = "spy"

    def __init__(self):
        self.resume_pdf = "UNSET"
        self.submit_called = False

    def is_available(self):
        return True

    def submit(self, application, answers, resume_pdf):
        self.submit_called = True
        self.resume_pdf = resume_pdf
        return {"status": SUBMIT_SUCCESS, "confirmation": "OK-1"}


# ---------------------------------------------------------------------------
# 1. The premise (mutation guard): the untailored production row shape
# ---------------------------------------------------------------------------
def test_untailored_row_has_no_tailoring_record_id(db):
    """The exact live shape: aggressive autopilot, tailoring OFF, zero
    tailoring_records — so anything keyed ONLY on tailoring_record_id is blind."""
    _upload_base_resume(USER)
    aid = _seat_approved(_create())
    row = ledger.get_application(USER, aid)

    assert row["tailoring_record_id"] is None
    assert db.tailoring_records.count_documents({"user_email": USER}) == 0


# ---------------------------------------------------------------------------
# 2. Untailored → the active BASE résumé is referenced (ids, never bytes)
# ---------------------------------------------------------------------------
def test_base_resume_is_referenced_when_untailored(db):
    rid = _upload_base_resume(USER)
    aid = _seat_approved(_create())
    row = ledger.get_application(USER, aid)

    ref = aas._resume_ref(row, USER)

    assert ref is not None, "no résumé referenced → nothing can ever be attached"
    assert ref["resume_asset_id"] == rid
    assert ref["user_email"] == USER
    # The orchestrator never carries megabytes: a REFERENCE only.
    assert all(isinstance(v, str) for v in ref.values())
    assert not any("bytes" in str(type(v)) for v in ref.values())


def test_submit_hands_the_base_reference_to_the_executor(db):
    rid = _upload_base_resume(USER)
    aid = _seat_approved(_create())

    spy = _SpyExecutor()
    aas.set_executor(spy)
    out = aas.submit(aid, USER)

    assert out["status"] == ledger.STATUS_SUBMITTED
    assert spy.submit_called is True
    assert spy.resume_pdf is not None, "executor.submit(resume_pdf=None) — the bug"
    assert spy.resume_pdf["resume_asset_id"] == rid
    assert spy.resume_pdf["user_email"] == USER


# ---------------------------------------------------------------------------
# 3. The tailored variant still WINS (fallback, not replacement)
# ---------------------------------------------------------------------------
def test_tailored_variant_wins_over_the_base_resume(db):
    _upload_base_resume(USER)
    row = {"tailoring_record_id": "trec-42"}

    ref = aas._resume_ref(row, USER)

    assert ref == {"tailoring_record_id": "trec-42", "user_email": USER}
    assert "resume_asset_id" not in ref


def test_pinned_key_falls_back_to_the_base_asset(db):
    """The remote fleet pulls its résumé from /executor/resume-url, which
    presigns THIS key — an untailored row must still name a real object."""
    rid = _upload_base_resume(USER)
    aid = _seat_approved(_create())

    assert aas.pinned_resume_s3_key(aid, USER) == f"resume-assets/{rid}/original"


def test_pinned_key_prefers_the_tailored_pdf(db):
    _upload_base_resume(USER)
    aid = _seat_approved(_create())
    db.tailoring_records.insert_one({
        "record_id": "trec-42", "user_email": USER,
        "current_version_id": "v1",
        "versions": [{"version_id": "v1",
                      "files": {"pdf": {"s3_key": "tailoring/trec-42/v1/0.pdf"}}}],
    })
    db.applications.update_one({"application_id": aid, "user_email": USER},
                               {"$set": {"tailoring_record_id": "trec-42"}})

    assert aas.pinned_resume_s3_key(aid, USER) == "tailoring/trec-42/v1/0.pdf"


# ---------------------------------------------------------------------------
# 4. No résumé at all → nothing resolves, and the drive PARKS (never submits)
# ---------------------------------------------------------------------------
def test_user_with_no_resume_resolves_to_nothing(db):
    aid = _seat_approved(_create())
    row = ledger.get_application(USER, aid)

    assert aas._resume_ref(row, USER) is None
    assert aas.pinned_resume_s3_key(aid, USER) is None


def test_asset_with_no_object_key_is_not_attachable(db):
    """Upload wrote the Mongo doc but the S3 put failed (the orphan rule's
    pending state) — there are no bytes to attach, so the row must ask, not
    reference a phantom object."""
    rid = resume_access.create_pending_asset(
        owner_type="user", user_email=USER, filename="half_uploaded.pdf")
    resume_access.adopt_active_if_unset(USER, rid)
    aid = _seat_approved(_create())
    row = ledger.get_application(USER, aid)

    assert db.resume_assets.find_one({"resume_id": rid})["object_key"] is None
    assert aas._resume_ref(row, USER) is None
    assert aas.pinned_resume_s3_key(aid, USER) is None


def test_user_with_no_resume_parks_instead_of_submitting(db, storage):
    """The pre-submit gate stays honest: a form with an upload control and
    nothing attached must ask, never send a CV-less application."""
    aid = _seat_approved(_create())
    row = ledger.get_application(USER, aid)

    resume = be._default_resume_resolver(_APP, aas._resume_ref(row, USER), USER)
    assert resume is None

    page = _page()
    ex = BrowserExecutor(screenshot_sink=lambda app, png: None, enabled=True)
    res = ex._drive_page(page, _ASHBY_URL, _CANONICAL_ANSWERS, resume, _APP,
                         ex._fill_fields)

    assert page.clicks == 0, "a CV-less application was SUBMITTED to an employer"
    assert res["status"] == SUBMIT_FAILED and res["error"] == "needs_input"
    assert res["field"] == "resume" and res["reason"] == "resume_missing"
    assert storage.keys == []


# ---------------------------------------------------------------------------
# 5. The executor resolves the base reference to real bytes and attaches them
# ---------------------------------------------------------------------------
def test_default_resolver_fetches_the_base_asset_bytes(db, storage):
    rid = _upload_base_resume(USER)

    resolved = be._default_resume_resolver(
        _APP, {"resume_asset_id": rid, "user_email": USER}, USER)

    assert resolved is not None
    name, data = resolved
    assert data == _ASSET_BYTES
    assert name == "Harshith_Manne_Cloud_Engineer.pdf"
    assert storage.keys == [f"resume-assets/{rid}/original"]


def test_base_resume_is_actually_attached_and_the_form_submits(db, storage):
    rid = _upload_base_resume(USER)
    resolved = be._default_resume_resolver(
        _APP, {"resume_asset_id": rid, "user_email": USER}, USER)

    page = _page()
    ex = BrowserExecutor(screenshot_sink=lambda app, png: None, enabled=True)
    res = ex._drive_page(page, _ASHBY_URL, _CANONICAL_ANSWERS, resolved, _APP,
                         ex._fill_fields)

    assert page.uploads, "the résumé never reached a file input"
    assert "Harshith_Manne_Cloud_Engineer.pdf" in set(page.uploads.values())
    assert res["status"] == SUBMIT_SUCCESS
    assert page.clicks == 1


def test_upload_name_keeps_the_real_extension(db, storage):
    rid = _upload_base_resume(
        USER, filename="../../etc/My Résumé v2.docx",
        mime_type="application/vnd.openxmlformats-officedocument."
                  "wordprocessingml.document")

    name, _data = be._default_resume_resolver(
        _APP, {"resume_asset_id": rid, "user_email": USER}, USER)

    assert name.endswith(".docx")
    assert "/" not in name and ".." not in name


# ---------------------------------------------------------------------------
# 6. OWNER SCOPING — a résumé is the most sensitive artifact in the product
# ---------------------------------------------------------------------------
def test_another_users_asset_id_never_resolves(db, storage):
    """A cross-user asset id presented on the executor payload must not resolve."""
    victim_rid = _upload_base_resume(OTHER, filename="Victim_Resume.pdf")

    resolved = be._default_resume_resolver(
        _APP, {"resume_asset_id": victim_rid, "user_email": USER}, USER)

    assert resolved is None, "cross-user résumé read"
    assert storage.keys == [], "S3 was touched for another user's object"


def test_asset_id_alone_never_resolves(db, storage):
    """No owner in scope ⇒ no résumé (never an unscoped id lookup)."""
    rid = _upload_base_resume(USER)

    assert be._default_resume_resolver(_APP, {"resume_asset_id": rid}, None) is None
    assert storage.keys == []


def test_get_owned_asset_is_owner_scoped(db):
    victim_rid = _upload_base_resume(OTHER, filename="Victim_Resume.pdf")

    assert resume_access.get_owned_asset(OTHER, victim_rid) is not None
    assert resume_access.get_owned_asset(USER, victim_rid) is None
    assert resume_access.get_owned_asset("", victim_rid) is None
    # Operator smuggling in the equality position is refused, not executed.
    assert resume_access.get_owned_asset({"$ne": None}, victim_rid) is None
    assert resume_access.get_owned_asset(USER, {"$ne": None}) is None


def test_pinned_key_is_owner_scoped(db):
    """The callback surface presigns off THIS key — it must never name another
    user's object, even when the caller passes a foreign application id."""
    _upload_base_resume(USER)
    victim_rid = _upload_base_resume(OTHER, filename="Victim_Resume.pdf")
    aid = _seat_approved(_create())

    key = aas.pinned_resume_s3_key(aid, USER)

    assert key == f"resume-assets/{aas._base_resume_ref(USER)['resume_asset_id']}/original"
    assert victim_rid not in key
    # A row that is not OTHER's resolves nothing for OTHER beyond their own asset.
    assert aas.pinned_resume_s3_key(aid, OTHER) == f"resume-assets/{victim_rid}/original"


# ---------------------------------------------------------------------------
# 7. The remote fleet is told a résumé IS available for an untailored row
# ---------------------------------------------------------------------------
def test_execution_envelope_reports_the_base_resume_available(db):
    aid = _seat_approved(_create())

    assert aas.execution_envelope(aid, USER)["resume_available"] is False

    _upload_base_resume(USER)

    assert aas.execution_envelope(aid, USER)["resume_available"] is True

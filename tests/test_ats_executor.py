"""Offline tests for the real auto-apply executor (ats_detector + ats_executor).

Runs fully offline (mongomock via conftest/_vg_support; no network — the
Greenhouse HTTP GET is faked with an injected session). Covers:

  * ats_detector — URL → (family, ids) for greenhouse/lever/ashby/workday/unknown.
  * Greenhouse recon parsing — type mapping, required flags, legal-attestation
    detection (err-toward-TRUE), résumé/cover-letter exclusion, EEOC/compliance.
  * recon_form contract — success shape (no `status`) vs. the clean
    EXECUTOR_UNAVAILABLE on network error / non-200 / bad JSON / other family.
  * submit / create_account / verify_state stay DARK (unavailable) and
    is_available() is False (rows stay `ready`).
  * end-to-end through auto_apply_service.gather() with a real mongomock ledger
    row and the executor injected via set_executor() — recon succeeds and the
    row leaves `queued`; submit() no-ops to unavailable leaving the row `ready`.
"""
import json

import pytest

from _vg_support import current_db

import services.application_ledger_service as als
import services.auto_apply_service as aas
from services.ats_detector import detect_ats, detect_family
from services.ats_executor import AtsExecutor, _parse_greenhouse
from services.executor_adapter import EXECUTOR_UNAVAILABLE, SUBMIT_WAITING_VERIFY, StubExecutor

USER_A = "alice@aspirely.me"
_JD = "We are hiring a backend engineer to build distributed systems in Python."


# ---------------------------------------------------------------------------
# Fake HTTP session (no network)
# ---------------------------------------------------------------------------
class _FakeResp:
    def __init__(self, status=200, payload=None, raise_exc=None, bad_json=False):
        self.status_code = status
        self._payload = payload
        self._raise = raise_exc
        self._bad_json = bad_json

    def json(self):
        if self._bad_json:
            raise ValueError("not json")
        return self._payload


class _FakeSession:
    def __init__(self, resp=None, exc=None):
        self.headers = {}
        self._resp = resp
        self._exc = exc
        self.calls = []

    def setdefault(self, *a, **k):  # headers.setdefault shim not needed; headers is dict
        pass

    def get(self, url, timeout=None):
        self.calls.append(url)
        if self._exc:
            raise self._exc
        return self._resp


# A representative Greenhouse job payload (shape mirrors the live API).
_GH_PAYLOAD = {
    "questions": [
        {"required": True, "label": "First Name",
         "fields": [{"name": "first_name", "type": "input_text"}]},
        {"required": True, "label": "Resume/CV",
         "fields": [{"name": "resume", "type": "input_file"},
                    {"name": "resume_text", "type": "textarea"}]},
        {"required": False, "label": "Cover Letter",
         "fields": [{"name": "cover_letter", "type": "input_file"},
                    {"name": "cover_letter_text", "type": "textarea"}]},
        {"required": True, "label": "Why do you want this role?",
         "fields": [{"name": "question_100", "type": "textarea"}]},
        {"required": True, "label": "Will you now or in the future require sponsorship?",
         "fields": [{"name": "question_200", "type": "multi_value_single_select",
                     "values": [{"label": "Yes", "value": 1}, {"label": "No", "value": 0}]}]},
        {"required": False, "label": "A hidden tracker",
         "fields": [{"name": "utm", "type": "input_hidden"}]},
    ],
    "compliance": [
        {"type": "eeoc", "questions": [
            {"required": False, "label": "Disability Status",
             "fields": [{"name": "disability_status", "type": "multi_value_single_select",
                         "values": [{"label": "Yes", "value": "1"}]}]}
        ]},
    ],
    "demographic_questions": None,
    "location_questions": [],
}


def _executor(resp=None, exc=None):
    return AtsExecutor(session=_FakeSession(resp=resp, exc=exc))


# ---------------------------------------------------------------------------
# ats_detector
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("url,family", [
    ("https://boards.greenhouse.io/gitlab/jobs/8565469002", "greenhouse"),
    ("https://job-boards.greenhouse.io/acme/jobs/123", "greenhouse"),
    ("https://jobs.lever.co/netflix/abc-123-def-456-7890", "lever"),
    ("https://jobs.ashbyhq.com/OpenAI/9f8e7d6c-1234-5678", "ashby"),
    ("https://acme.wd1.myworkdayjobs.com/en-US/careers/job/123", "workday"),
    ("https://careers-acme.icims.com/jobs/4567/x/job", "icims"),
    ("https://example.com/careers/apply", None),
    ("", None),
    (None, None),
])
def test_detect_family(url, family):
    assert detect_family(url) == family


def test_detect_greenhouse_ids():
    fam, ids = detect_ats("https://boards.greenhouse.io/gitlab/jobs/8565469002", None)
    assert fam == "greenhouse"
    assert ids["board_token"] == "gitlab"
    assert ids["job_id"] == "8565469002"


def test_detect_greenhouse_embed():
    fam, ids = detect_ats("https://boards.greenhouse.io/embed/job_app?for=acme&gh_jid=99887", None)
    assert fam == "greenhouse"
    assert ids["job_id"] == "99887"


def test_detect_lever_ids():
    fam, ids = detect_ats("https://jobs.lever.co/netflix/a1b2c3d4e5f6a7b8", None)
    assert fam == "lever"
    assert ids["site"] == "netflix"
    assert ids["posting_id"] == "a1b2c3d4e5f6a7b8"


def test_detect_ashby_ids():
    fam, ids = detect_ats("https://jobs.ashbyhq.com/OpenAI/1234abcd-5678-90ef-1234", None)
    assert fam == "ashby"
    assert ids["org"] == "OpenAI"


def test_detect_unknown_honors_declared_family():
    fam, ids = detect_ats("https://weird-internal-portal.example/apply", "workday")
    assert fam == "workday"
    assert ids == {}


# ---------------------------------------------------------------------------
# Greenhouse recon parsing
# ---------------------------------------------------------------------------
def test_parse_greenhouse_excludes_resume_and_cover_letter():
    qs = _parse_greenhouse(_GH_PAYLOAD)
    ids = {q["id"] for q in qs}
    assert not any(i.startswith(("resume", "cover_letter")) for i in ids)


def test_parse_greenhouse_drops_hidden_fields():
    qs = _parse_greenhouse(_GH_PAYLOAD)
    assert "utm" not in {q["id"] for q in qs}


def test_parse_greenhouse_type_mapping_and_required():
    qs = {q["id"]: q for q in _parse_greenhouse(_GH_PAYLOAD)}
    assert qs["first_name"]["type"] == "text" and qs["first_name"]["required"] is True
    assert qs["question_100"]["type"] == "textarea"
    assert qs["question_200"]["type"] == "select"


def test_parse_greenhouse_flags_legal_attestations():
    qs = {q["id"]: q for q in _parse_greenhouse(_GH_PAYLOAD)}
    # sponsorship (main form) + disability (eeoc compliance) → legal
    assert qs["question_200"]["is_legal_attestation"] is True
    assert qs["disability_status"]["is_legal_attestation"] is True
    # a plain screening question is NOT legal
    assert qs["question_100"]["is_legal_attestation"] is False


def test_parse_greenhouse_first_name_not_legal():
    qs = {q["id"]: q for q in _parse_greenhouse(_GH_PAYLOAD)}
    assert qs["first_name"]["is_legal_attestation"] is False


# ---------------------------------------------------------------------------
# recon_form contract
# ---------------------------------------------------------------------------
def test_recon_form_success_shape():
    ex = _executor(resp=_FakeResp(200, _GH_PAYLOAD))
    res = ex.recon_form("https://boards.greenhouse.io/acme/jobs/42", "greenhouse")
    assert "status" not in res                    # success = no status key
    assert res["needs_account"] is False
    assert len(res["questions"]) >= 3
    for q in res["questions"]:
        # `options` is present ONLY on enumerated fields (see
        # test_greenhouse_recon_carries_option_labels) — everything else is the
        # fixed ReconQuestion shape.
        assert set(q) - {"options"} == {
            "id", "text", "type", "required", "is_legal_attestation"}


def test_recon_form_network_error_is_unavailable():
    import requests
    ex = _executor(exc=requests.RequestException("boom"))
    res = ex.recon_form("https://boards.greenhouse.io/acme/jobs/42", "greenhouse")
    assert res["status"] == EXECUTOR_UNAVAILABLE
    assert res["questions"] == []


def test_recon_form_non_200_is_unavailable():
    ex = _executor(resp=_FakeResp(404, None))
    res = ex.recon_form("https://boards.greenhouse.io/acme/jobs/42", "greenhouse")
    assert res["status"] == EXECUTOR_UNAVAILABLE


def test_recon_form_bad_json_is_unavailable():
    ex = _executor(resp=_FakeResp(200, None, bad_json=True))
    res = ex.recon_form("https://boards.greenhouse.io/acme/jobs/42", "greenhouse")
    assert res["status"] == EXECUTOR_UNAVAILABLE


def test_recon_form_other_family_is_unavailable():
    ex = _executor(resp=_FakeResp(200, _GH_PAYLOAD))
    res = ex.recon_form("https://jobs.lever.co/acme/uuid-here-1234-5678", "lever")
    assert res["status"] == EXECUTOR_UNAVAILABLE
    assert ex._session.calls == []  # no HTTP attempted for unsupported family


def test_recon_form_greenhouse_listing_without_job_id_is_unavailable():
    ex = _executor(resp=_FakeResp(200, _GH_PAYLOAD))
    res = ex.recon_form("https://boards.greenhouse.io/gitlab", "greenhouse")
    assert res["status"] == EXECUTOR_UNAVAILABLE


# ---------------------------------------------------------------------------
# submit / oracle hooks stay dark
# ---------------------------------------------------------------------------
def test_is_available_false():
    assert _executor().is_available() is False


def test_submit_is_unavailable_and_never_raises():
    res = _executor().submit({"application_id": "x"}, {}, None)
    assert res["status"] == EXECUTOR_UNAVAILABLE
    assert res["confirmation"] is None and res["wall"] is None


def test_oracle_hooks_unavailable():
    ex = _executor()
    assert ex.create_account("u@x.co", "greenhouse", None, {})["status"] == EXECUTOR_UNAVAILABLE
    assert ex.verify_state({"application_id": "x"}, {})["status"] == EXECUTOR_UNAVAILABLE


# ---------------------------------------------------------------------------
# End-to-end through the orchestrator (mongomock ledger + injected executor)
# ---------------------------------------------------------------------------
@pytest.fixture()
def led_db():
    d = current_db()
    for c in ("applications", "auto_apply_answers", "auto_apply_freezes",
              "auto_submit_audit"):
        d[c].delete_many({})
    d.applications.create_index(
        [("user_email", 1), ("job_identity", 1)], unique=True,
        name="user_email_job_identity_unique")
    d.applications.create_index(
        [("application_id", 1)], unique=True, name="application_id_unique")
    return d


@pytest.fixture(autouse=True)
def _reset_executor():
    yield
    aas.set_executor(None)  # clear the module-global between tests


def test_gather_with_real_recon_leaves_queued_state(led_db):
    view, outcome = als.create_application(
        USER_A, company="GitLab", title="AI Engineer", jd_text=_JD,
        source="greenhouse", ats_family="greenhouse",
        apply_url="https://boards.greenhouse.io/gitlab/jobs/8565469002")
    aid = view["application_id"]

    aas.set_executor(AtsExecutor(session=_FakeSession(resp=_FakeResp(200, _GH_PAYLOAD))))
    result = aas.gather(aid, USER_A)

    # Recon succeeded → NOT the unavailable no-op path.
    assert result.get("status") != EXECUTOR_UNAVAILABLE
    row = als.get_application(USER_A, aid)
    # F: the clean-auto landing is now ready_for_review → approved (was `ready`);
    # an unresolved required/legal question still routes to needs_input.
    assert row["status"] in (als.STATUS_READY_FOR_REVIEW, als.STATUS_APPROVED,
                             als.STATUS_NEEDS_INPUT)


def test_immediate_submit_enqueues_only_ready_rows(led_db, monkeypatch):
    # A ready row and a needs_input row.
    ready, _ = als.create_application(
        USER_A, company="A", title="Eng", jd_text=_JD, source="greenhouse",
        apply_url="https://boards.greenhouse.io/a/jobs/1")
    r_aid = ready["application_id"]
    # F: submit is enqueued from `approved` (not `ready`) — seat the row there.
    als.mark_gathering(USER_A, r_aid)
    als.mark_ready_for_review(USER_A, r_aid, autonomy="auto", confidence_score=0.95,
                              tailoring_record_id=None)
    als.approve(USER_A, [r_aid], approved_by="system")

    pending, _ = als.create_application(
        USER_A, company="B", title="Eng", jd_text=_JD + " variant", source="greenhouse",
        apply_url="https://boards.greenhouse.io/b/jobs/2")
    p_aid = pending["application_id"]
    als.mark_gathering(USER_A, p_aid)
    als.mark_needs_input(USER_A, p_aid, [{"id": "q", "text": "?"}],
                         autonomy="ask", confidence_score=0.5)

    calls = []
    monkeypatch.setattr(aas, "_auto_submit_enabled", lambda: True)
    monkeypatch.setattr(aas, "_enqueue_submit", lambda aid, ue: (calls.append(aid), "job")[1])

    n = aas.request_immediate_submit(USER_A, [r_aid, p_aid])
    assert n == 1 and calls == [r_aid]  # only the ready row


def test_immediate_submit_gated_by_master_switch(led_db, monkeypatch):
    ready, _ = als.create_application(
        USER_A, company="A", title="Eng", jd_text=_JD, source="greenhouse",
        apply_url="https://boards.greenhouse.io/a/jobs/1")
    aid = ready["application_id"]
    # F: seat the row in `approved` (submit's pickup state).
    als.mark_gathering(USER_A, aid)
    als.mark_ready_for_review(USER_A, aid, autonomy="auto", confidence_score=0.95,
                              tailoring_record_id=None)
    als.approve(USER_A, [aid], approved_by="system")

    calls = []
    monkeypatch.setattr(aas, "_auto_submit_enabled", lambda: False)  # kill-switch OFF
    monkeypatch.setattr(aas, "_enqueue_submit", lambda aid, ue: calls.append(aid))
    assert aas.request_immediate_submit(USER_A, [aid]) == 0
    assert calls == []


class _CaptchaExecutor(StubExecutor):
    """Available executor that reports a CAPTCHA wall on submit."""
    def is_available(self):
        return True
    def submit(self, app, answers, resume_pdf):
        return {"status": SUBMIT_WAITING_VERIFY, "confirmation": None,
                "screenshot_ref": None, "wall": {"kind": "captcha", "needs": ["captcha"]},
                "error": None}


def test_captcha_wall_surfaces_apply_url_for_human_handoff(led_db):
    view, _ = als.create_application(
        USER_A, company="A", title="Eng", jd_text=_JD, source="greenhouse",
        ats_family="greenhouse", apply_url="https://boards.greenhouse.io/a/jobs/1")
    aid = view["application_id"]
    # F: submit runs from `approved` — seat the row there before the captcha wall.
    als.mark_gathering(USER_A, aid)
    als.mark_ready_for_review(USER_A, aid, autonomy="auto", confidence_score=0.95,
                              tailoring_record_id=None)
    als.approve(USER_A, [aid], approved_by="system")

    aas.set_executor(_CaptchaExecutor())
    res = aas.submit(aid, USER_A)
    assert res["status"] == "waiting_verify"
    row = als.get_application(USER_A, aid)
    assert row["status"] == als.STATUS_NEEDS_INPUT
    # The user-facing item carries the apply URL so the FE can deep-link them to
    # finish the challenge in their own browser (human-in-the-loop strategy).
    item = next(i for i in row["needs_input"] if i.get("kind") == "oracle_captcha")
    assert item["apply_url"] == "https://boards.greenhouse.io/a/jobs/1"
    assert item["action"] == "complete_in_browser"


def test_submit_leaves_row_ready_when_fleet_unavailable(led_db):
    view, _ = als.create_application(
        USER_A, company="GitLab", title="AI Engineer", jd_text=_JD,
        source="greenhouse", ats_family="greenhouse",
        apply_url="https://boards.greenhouse.io/gitlab/jobs/8565469002")
    aid = view["application_id"]
    # F: force the row to `approved` (submit's entry state) so submit() is reached.
    als.mark_gathering(USER_A, aid)
    als.mark_ready_for_review(USER_A, aid, autonomy="auto", confidence_score=0.95,
                              tailoring_record_id=None)
    als.approve(USER_A, [aid], approved_by="system")

    aas.set_executor(AtsExecutor(session=_FakeSession(resp=_FakeResp(200, _GH_PAYLOAD))))
    result = aas.submit(aid, USER_A)

    # is_available() is False → executor never grabs the row; stays approved.
    assert result["status"] == EXECUTOR_UNAVAILABLE
    row = als.get_application(USER_A, aid)
    assert row["status"] == als.STATUS_APPROVED

"""Offline tests for P1-APPROVE-GET (control C17) + P1-ENC (control C8/C15).

P1-APPROVE-GET — the emailed magic-link approval must be a NON-MUTATING GET plus
an authenticated, CSRF-safe POST that actually consumes the single-use token:

  * a GET on ``/api/apply/approve-link`` NEVER mutates — a `review` link 302s into
    the app; an approve/skip link 302s to the confirmation surface carrying the
    STILL-UNCLAIMED token (row stays ``ready_for_review``, token stays unused);
  * a bare GET therefore cannot approve (a prefetch/scanner/CSRF is inert);
  * the authenticated ``POST /api/apply/approve-link`` (JWT Bearer — an attacker
    page cannot forge the Authorization header) consumes the token and approves;
  * the token is single-use (a second POST is 409, never 401), and the token's
    owner MUST match the JWT identity (mismatch → 403, never 401).

P1-ENC — the executor receipt PUT is pinned to the apply CMK (SSE-KMS): the
presigned PUT ``issue_receipt_put`` mints carries the ``x-amz-server-side-
encryption`` + key-id headers (mirroring the mail raw-MIME PUT).

Runs fully offline (mongomock via conftest/_vg_support; no Bedrock / AWS / network).
"""
import pytest

from _vg_support import load_service_app, current_db

import services.application_ledger_service as ledger
import services.auto_apply_service as aas
import services.approval_token_service as ats
import services.executor_capability_service as caps

USER = "alice@aspirely.me"
OTHER = "bob@aspirely.me"
_JD = "We are hiring a backend engineer to build distributed systems in Python."

_COLLECTIONS = (
    "applications", "auto_submit_audit", "auto_apply_answers",
    "auto_apply_freezes", "approval_tokens", "executor_capabilities",
)


@pytest.fixture()
def db():
    d = current_db()
    for c in _COLLECTIONS:
        d[c].delete_many({})
    d.applications.create_index(
        [("user_email", 1), ("job_identity", 1)], unique=True,
        name="user_email_job_identity_unique")
    d.applications.create_index(
        [("application_id", 1)], unique=True, name="application_id_unique")
    return d


@pytest.fixture()
def app(monkeypatch):
    monkeypatch.setenv("AUTO_SUBMIT_ENABLED", "true")
    return load_service_app("jobs")


@pytest.fixture(autouse=True)
def _reset_executor():
    yield
    aas.set_executor(None)


def _token(flask_app, identity):
    from flask_jwt_extended import create_access_token
    with flask_app.app_context():
        return create_access_token(identity=identity)


def _auth_client(flask_app, identity):
    tok = _token(flask_app, identity)
    c = flask_app.test_client()

    def call(method, path, json=None):
        return getattr(c, method)(
            path, headers={"Authorization": f"Bearer {tok}"}, json=json)

    return call


def _plain_client(flask_app):
    """No Authorization header — models the raw email-link GET (public route)."""
    return flask_app.test_client()


def _seat_ready_for_review(aid, user=USER):
    ledger.mark_gathering(user, aid)
    aas._persist_answers(aid, user, {"email": "a@b.co"}, 1.0, False,
                         autonomy="ask", confidence_score=0.4,
                         recon_fingerprint="recon-1", resume_version_id="rv-1")
    ledger.mark_ready_for_review(user, aid, autonomy="ask", confidence_score=0.4)


def _create(user=USER):
    view, _ = ledger.create_application(
        user, company="Acme", title="Software Engineer", jd_text=_JD,
        source="greenhouse", ats_family="greenhouse",
        apply_url="https://boards.greenhouse.io/x/jobs/1")
    return view["application_id"]


def _token_used(db, jti_of_action_token) -> bool:
    rec = db.approval_tokens.find_one({"jti": jti_of_action_token})
    return bool((rec or {}).get("used"))


# ---------------------------------------------------------------------------
# 1. The GET is NON-MUTATING (bare GET cannot approve)
# ---------------------------------------------------------------------------
def test_get_approve_link_does_not_mutate_row_stays_ready_for_review(db, app):
    aid = _create()
    _seat_ready_for_review(aid)
    token = ats.make_token("approve_all", USER, [aid])

    client = _plain_client(app)
    resp = client.get(f"/api/apply/approve-link?token={token}")

    # 302 to the confirmation surface, carrying the token — never mutating.
    assert resp.status_code == 302
    assert "/app/apply" in resp.headers["Location"]
    assert "approve=" in resp.headers["Location"]

    # The row is UNTOUCHED and no snapshot was frozen.
    assert ledger.get_application(USER, aid)["status"] == ledger.STATUS_READY_FOR_REVIEW
    assert ledger.get_approval_snapshot(USER, aid) is None
    # The single-use token was NOT consumed — the POST can still claim it.
    row = db.approval_tokens.find_one({"user_email": USER})
    assert row is not None and row["used"] is False


def test_get_review_link_redirects_without_token_param(db, app):
    aid = _create()
    _seat_ready_for_review(aid)
    token = ats.make_token("review", USER, [aid])
    resp = _plain_client(app).get(f"/api/apply/approve-link?token={token}")
    assert resp.status_code == 302
    loc = resp.headers["Location"]
    assert loc.endswith("/app/apply")   # review carries no ?approve token
    # A review GET never mutates and never consumes the token.
    row = db.approval_tokens.find_one({"user_email": USER})
    assert row["used"] is False


def test_get_invalid_token_shows_page_not_401(db, app):
    resp = _plain_client(app).get("/api/apply/approve-link?token=garbage.sig")
    assert resp.status_code == 403          # invalid_link — never 401
    assert resp.mimetype == "text/html"


# ---------------------------------------------------------------------------
# 2. The authenticated POST consumes the token + approves (CSRF-safe)
# ---------------------------------------------------------------------------
def test_post_with_token_approves_and_freezes_snapshot(db, app):
    aid = _create()
    _seat_ready_for_review(aid)
    token = ats.make_token("approve_all", USER, [aid])

    call = _auth_client(app, USER)
    resp = call("post", "/api/apply/approve-link", {"token": token})

    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    assert body["ok"] is True
    assert body["approved"] == 1

    # The row is now approved with a frozen snapshot (F intact).
    assert ledger.get_application(USER, aid)["status"] == ledger.STATUS_APPROVED
    snap = ledger.get_approval_snapshot(USER, aid)
    assert snap is not None and snap["approved_by"] == "user"

    # The single-use token was consumed by the POST.
    assert db.approval_tokens.find_one({"user_email": USER})["used"] is True


def test_post_token_is_single_use_second_post_is_409(db, app):
    aid = _create()
    _seat_ready_for_review(aid)
    token = ats.make_token("approve_all", USER, [aid])

    call = _auth_client(app, USER)
    assert call("post", "/api/apply/approve-link", {"token": token}).status_code == 200
    # A replayed POST with the SAME token → 409 (already used), NEVER 401.
    resp2 = call("post", "/api/apply/approve-link", {"token": token})
    assert resp2.status_code == 409
    assert resp2.get_json()["error"] == "link_already_used"


def test_post_owner_mismatch_is_403_not_401(db, app):
    # Token minted for USER, but a DIFFERENT authenticated account posts it.
    aid = _create()
    _seat_ready_for_review(aid)
    token = ats.make_token("approve_all", USER, [aid])

    call = _auth_client(app, OTHER)
    resp = call("post", "/api/apply/approve-link", {"token": token})
    assert resp.status_code == 403
    assert resp.get_json()["error"] == "not_authorized"
    # The other account's mismatched attempt did NOT approve USER's row.
    assert ledger.get_application(USER, aid)["status"] == ledger.STATUS_READY_FOR_REVIEW


def test_post_without_token_is_400(db, app):
    call = _auth_client(app, USER)
    resp = call("post", "/api/apply/approve-link", {})
    assert resp.status_code == 400


def test_post_requires_jwt_bare_post_is_401(db, app):
    # The POST is @jwt_required — a MISSING JWT is the ONE legitimate 401 (the
    # decorator's contract), distinct from any business failure. This confirms the
    # mutation cannot happen without an authenticated session.
    aid = _create()
    _seat_ready_for_review(aid)
    token = ats.make_token("approve_all", USER, [aid])
    resp = _plain_client(app).post("/api/apply/approve-link", json={"token": token})
    assert resp.status_code == 401              # missing token, not a business error
    assert ledger.get_application(USER, aid)["status"] == ledger.STATUS_READY_FOR_REVIEW


def test_post_skip_token_skips(db, app):
    aid = _create()
    ledger.mark_gathering(USER, aid)          # a pre-submit, skippable state
    token = ats.make_token("skip", USER, [aid])
    call = _auth_client(app, USER)
    resp = call("post", "/api/apply/approve-link", {"token": token})
    assert resp.status_code == 200
    assert ledger.get_application(USER, aid)["status"] == ledger.STATUS_SKIPPED


# ---------------------------------------------------------------------------
# 3. verify() peek is non-consuming; verify_and_claim consumes
# ---------------------------------------------------------------------------
def test_verify_peek_does_not_consume_then_claim_does(db):
    token = ats.make_token("approve_one", USER, ["app-x"])
    # Peek twice — still claimable.
    assert ats.verify(token)["action"] == "approve_one"
    assert ats.verify(token)["action"] == "approve_one"
    row = db.approval_tokens.find_one({"user_email": USER})
    assert row["used"] is False
    # Claim consumes it exactly once.
    assert ats.verify_and_claim(token)["action"] == "approve_one"
    assert db.approval_tokens.find_one({"user_email": USER})["used"] is True
    with pytest.raises(ats.TokenAlreadyUsed):
        ats.verify_and_claim(token)


# ===========================================================================
# 4. P1-ENC — the executor receipt PUT is pinned to the apply CMK (SSE-KMS)
# ===========================================================================
class _FakeS3Client:
    """Captures the presign Params so the test can assert the SSE-KMS pinning."""

    def __init__(self):
        self.presign_params = None

    def generate_presigned_url(self, op, Params=None, ExpiresIn=None):
        self.presign_params = {"op": op, "Params": Params, "ExpiresIn": ExpiresIn}
        return "https://s3.example.test/audit/receipt.png?sig=x"


def test_receipt_presign_carries_sse_kms_header(db, monkeypatch):
    monkeypatch.setenv("RESUME_S3_BUCKET", "jobpilot-resumes-test")
    monkeypatch.setenv("APPLY_MAIL_KMS_KEY_ID", "arn:aws:kms:us-east-1:1:key/apply-cmk")

    # Wire the storage singleton to a capturing fake client (no AWS).
    from services import s3_service
    storage = s3_service.get_storage_service()
    fake = _FakeS3Client()
    monkeypatch.setattr(storage, "_client", fake)
    monkeypatch.setattr(storage, "_bucket", "jobpilot-resumes-test")

    # A real minted + claimed capability (the one-use receipt flag lives on it).
    cap = caps.mint_capability(execution_id="enc-e1", user_email=USER,
                               application_id="app-enc")
    doc = caps.claim_capability(cap)["doc"]

    put = caps.issue_receipt_put(doc)

    # The returned headers the worker MUST echo pin the object to our CMK.
    hdrs = put["headers"]
    assert hdrs["x-amz-server-side-encryption"] == "aws:kms"
    assert hdrs["x-amz-server-side-encryption-aws-kms-key-id"] == \
        "arn:aws:kms:us-east-1:1:key/apply-cmk"
    # And the SIGNED policy carries the same SSE-KMS params (so S3 enforces it).
    params = fake.presign_params["Params"]
    assert params["ServerSideEncryption"] == "aws:kms"
    assert params["SSEKMSKeyId"] == "arn:aws:kms:us-east-1:1:key/apply-cmk"
    assert put["key"].startswith("auto-apply-audit/app-enc/enc-e1/")
    # One-use: a second issuance is refused.
    with pytest.raises(caps.CapabilityError) as ei:
        caps.issue_receipt_put(doc)
    assert ei.value.status == 409

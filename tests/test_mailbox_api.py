"""Managed Application Mailbox — user-facing READ/CONTROL API (managed-mailbox-v1 §5).

Drives the real Flask routes on the resume Lambda app, fully offline (mongomock
+ a FakeStorage S3; no AWS, no Bedrock, no network). Proves the §5 contract:

  * provision is idempotent AND resolves the readable name SERVER-SIDE (a body
    ``display_name`` is IGNORED — spec D7);
  * GET /mailbox before/after provision;
  * threads + thread detail are owner-scoped — another user's thread_id is a 404
    (never 403 / never a leak — spec §10 IDOR);
  * MessageView never carries raw MIME: sanitized text inline + a short-TTL
    presigned ``safe_html_url``;
  * attachments: clean -> download_url; suspicious/blocked -> 403;
  * attach validates application OWNERSHIP (foreign application_id -> 404);
  * read toggles unread_count; report-phishing flips trust + the flag;
  * every endpoint: no JWT -> 401 (from @jwt_required only); every BUSINESS
    failure is 400/403/404 — NEVER 401.
"""
from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

import services.managed_mailbox_service as mailbox
import services.mail_store_service as store
from _vg_support import load_service_app

DOMAIN = "pilotmail.co"
OWNER = "owner@x.com"
OTHER = "other@x.com"
OWNER_ADDR = "harshith.manne@pilotmail.co"
OTHER_ADDR = "other.person@pilotmail.co"
FIX = Path(__file__).resolve().parent / "fixtures" / "mail"

_COLLECTIONS = (
    "managed_mailboxes", "mail_aliases", "mail_threads", "mail_messages",
    "mail_attachments", "mail_commit_tokens", "tailoring_records",
    "applications", "users",
)


def _db():
    from utils.db_connect import DBConnect
    return DBConnect().get_db()


def _eml(name: str) -> bytes:
    return (FIX / name).read_bytes()


class FakeStorage:
    """In-memory S3: captures put_object_bytes, serves get_object_bytes, and
    hands back deterministic fake presigned GET/PUT URLs (no AWS)."""

    def __init__(self):
        self.objects = {}

    def put_object_bytes(self, key, body, *, bucket=None, content_type="application/octet-stream", kms_key_id=None):
        self.objects[key] = body
        return key

    def get_object_bytes(self, key, *, bucket=None):
        if key not in self.objects:
            raise KeyError(key)
        return self.objects[key]

    def generate_presigned_put(self, key, *, bucket=None, content_type=None, kms_key_id=None, expiry=900):
        return {"url": f"https://s3.local/{key}", "headers": {}}

    def generate_presigned_get(self, key, *, bucket=None, expiry=300):
        return f"https://s3.local/get/{key}?ttl={expiry}"

    def delete_object(self, key, *, bucket=None):
        self.objects.pop(key, None)
        return True


@pytest.fixture()
def env(monkeypatch):
    monkeypatch.setenv("USE_SSM_SECRETS", "false")
    monkeypatch.setenv("APPLY_MAIL_DOMAIN", DOMAIN)
    monkeypatch.setenv("APPLY_MAIL_BUCKET", "jobpilot-mail-test")
    monkeypatch.setenv("APPLY_MAIL_KMS_KEY_ID", "test-cmk")
    db = _db()
    for c in _COLLECTIONS:
        db[c].delete_many({})
    mailbox.ensure_indexes()
    store.ensure_indexes()
    return monkeypatch


@pytest.fixture()
def fake_s3(monkeypatch):
    fake = FakeStorage()
    import services.s3_service as s3
    monkeypatch.setattr(s3, "get_storage_service", lambda: fake)
    return fake


@pytest.fixture()
def app(env, fake_s3):
    return load_service_app("resume")


def _hdr(app, email):
    from flask_jwt_extended import create_access_token
    with app.app_context():
        token = create_access_token(identity=email)
    return {"Authorization": f"Bearer {token}"}


def _ingest(email, name, addr, eml_name, mid, frm="hr@corp.example"):
    """Provision + resolve + ingest a real .eml the way the commit route does."""
    mailbox.provision(email, name)
    info = mailbox.resolve_alias(addr)
    return store.ingest_committed(info, addr, frm, _eml(eml_name), mid)


def _seed_application(email, record_id, domain="greenhouse.io", hours_before=1):
    _db().tailoring_records.insert_one({
        "record_id": record_id,
        "user_email": email,
        "application": {
            "status": "applied",
            "employer_domain": domain,
            "applied_at": store._now() - timedelta(hours=hours_before),
        },
        "jd_analysis": {"company": "Acme"},
    })


# ---------------------------------------------------------------------------
# GET /mailbox before/after provision
# ---------------------------------------------------------------------------

def test_get_mailbox_self_heals_when_unprovisioned(app):
    # Spec §6: provisioning is automatic — a legacy account with no mailbox is
    # provisioned on read rather than being shown a "create my address" prompt.
    resp = app.test_client().get("/api/resume/mailbox", headers=_hdr(app, OWNER))
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["provisioned"] is True
    assert body["address"].endswith("@pilotmail.co")
    assert body["forward_to"] == OWNER
    assert body["forward_to_verified"] is True


def test_get_mailbox_after_provision(app):
    mailbox.provision(OWNER, "Harshith Manne")
    resp = app.test_client().get("/api/resume/mailbox", headers=_hdr(app, OWNER))
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["provisioned"] is True
    assert body["address"] == OWNER_ADDR
    assert body["unread_count"] == 0


# ---------------------------------------------------------------------------
# provision — idempotent + name resolved SERVER-SIDE (body display_name IGNORED)
# ---------------------------------------------------------------------------

def test_provision_name_is_server_side_body_ignored(app):
    # The user's real name lives on their account record.
    _db().users.insert_one({"email": OWNER, "name": "Harshith Manne"})
    client = app.test_client()
    # The request body TRIES to spoof a different name — it must be ignored (D7).
    resp = client.post(
        "/api/resume/mailbox/provision",
        json={"display_name": "Evil Hacker", "privacy_alias": False},
        headers=_hdr(app, OWNER),
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["provisioned"] is True
    assert body["address"] == OWNER_ADDR          # server name, NOT the body's
    assert "evil.hacker" not in body["address"]


def test_provision_is_idempotent(app):
    _db().users.insert_one({"email": OWNER, "name": "Harshith Manne"})
    client = app.test_client()
    a = client.post("/api/resume/mailbox/provision", json={}, headers=_hdr(app, OWNER))
    b = client.post("/api/resume/mailbox/provision", json={}, headers=_hdr(app, OWNER))
    assert a.status_code == 200 and b.status_code == 200
    assert a.get_json()["address"] == b.get_json()["address"] == OWNER_ADDR
    assert _db().managed_mailboxes.count_documents({"user_email": OWNER}) == 1


def test_provision_falls_back_to_privacy_when_no_name(app):
    # No users record, no resume -> no resolvable name -> opaque privacy alias.
    import re
    resp = app.test_client().post(
        "/api/resume/mailbox/provision", json={}, headers=_hdr(app, OWNER)
    )
    assert resp.status_code == 200
    assert re.match(r"^career\.[0-9a-f]{6}@pilotmail\.co$", resp.get_json()["address"])


# ---------------------------------------------------------------------------
# pause + rotate
# ---------------------------------------------------------------------------

def test_pause_toggles_state(app):
    mailbox.provision(OWNER, "Harshith Manne")
    client = app.test_client()
    resp = client.post("/api/resume/mailbox/pause", json={"paused": True}, headers=_hdr(app, OWNER))
    assert resp.status_code == 200
    assert resp.get_json()["paused"] is True
    assert mailbox.get_mailbox(OWNER)["paused"] is True


def test_pause_missing_body_is_400_not_401(app):
    mailbox.provision(OWNER, "Harshith Manne")
    resp = app.test_client().post("/api/resume/mailbox/pause", json={}, headers=_hdr(app, OWNER))
    assert resp.status_code == 400
    assert resp.status_code != 401


def test_pause_without_mailbox_is_404_not_401(app):
    resp = app.test_client().post(
        "/api/resume/mailbox/pause", json={"paused": True}, headers=_hdr(app, "nomailbox@x.com")
    )
    assert resp.status_code == 404
    assert resp.status_code != 401


def test_rotate_mints_new_address(app):
    _db().users.insert_one({"email": OWNER, "name": "Harshith Manne"})
    a = mailbox.provision(OWNER, "Harshith Manne")
    resp = app.test_client().post("/api/resume/mailbox/rotate", json={}, headers=_hdr(app, OWNER))
    assert resp.status_code == 200
    new_addr = resp.get_json()["address"]
    assert new_addr != a["address"]
    assert new_addr.startswith("harshith.manne")
    # Old local part is tombstoned (never recycled).
    assert _db().mail_aliases.find_one({"local_part": "harshith.manne"})["state"] == "retired"


# ---------------------------------------------------------------------------
# threads — owner-scoped listing
# ---------------------------------------------------------------------------

def test_threads_are_owner_scoped(app):
    _ingest(OWNER, "Harshith Manne", OWNER_ADDR, "pdf_attachment.eml", "<o-1@corp>")
    _ingest(OTHER, "Other Person", OTHER_ADDR, "pdf_attachment.eml", "<x-1@corp>")

    resp = app.test_client().get("/api/resume/mailbox/threads", headers=_hdr(app, OWNER))
    assert resp.status_code == 200
    threads = resp.get_json()["threads"]
    assert len(threads) == 1                       # only OWNER's thread
    assert all(t.get("unread_count", 0) >= 0 for t in threads)


# ---------------------------------------------------------------------------
# thread detail — MessageView (sanitized text inline + presigned html, no raw)
# ---------------------------------------------------------------------------

def test_thread_detail_messageview_no_raw_mime(app):
    res = _ingest(OWNER, "Harshith Manne", OWNER_ADDR, "xss.eml", "<xss-1@corp>")
    resp = app.test_client().get(
        f"/api/resume/mailbox/threads/{res['thread_id']}", headers=_hdr(app, OWNER)
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["thread"]["thread_id"] == res["thread_id"]
    msg = body["messages"][0]

    # Sanitized text is inline; the sanitized HTML is a short-TTL presigned URL.
    assert isinstance(msg["text"], str)
    assert msg["safe_html_url"].startswith("https://s3.local/get/mail/html/")

    # NEVER the raw MIME: no raw key, no internal s3 key fields, no <script.
    assert "raw_s3_key" not in msg
    assert "safe_html_s3_key" not in msg
    assert "text_s3_key" not in msg
    assert "<script" not in msg["text"].lower()

    # The evidence fields the inbox needs are present.
    for k in ("from", "subject", "received_at", "spf", "dkim", "dmarc", "trust", "read", "attachments"):
        assert k in msg


def test_foreign_thread_id_is_404_not_leak(app):
    other = _ingest(OTHER, "Other Person", OTHER_ADDR, "pdf_attachment.eml", "<x-1@corp>")
    mailbox.provision(OWNER, "Harshith Manne")
    # OWNER asks for OTHER's thread — 404 (never 403, never the content).
    resp = app.test_client().get(
        f"/api/resume/mailbox/threads/{other['thread_id']}", headers=_hdr(app, OWNER)
    )
    assert resp.status_code == 404
    assert resp.status_code != 401


# ---------------------------------------------------------------------------
# attachments — clean -> url; suspicious/blocked -> 403
# ---------------------------------------------------------------------------

def _first_attachment_id(app, email, thread_id):
    detail = app.test_client().get(
        f"/api/resume/mailbox/threads/{thread_id}", headers=_hdr(app, email)
    ).get_json()
    return detail["messages"][0]["attachments"][0]["id"]


def test_clean_attachment_returns_download_url(app):
    res = _ingest(OWNER, "Harshith Manne", OWNER_ADDR, "pdf_attachment.eml", "<o-1@corp>")
    aid = _first_attachment_id(app, OWNER, res["thread_id"])
    resp = app.test_client().get(
        f"/api/resume/mailbox/attachments/{aid}", headers=_hdr(app, OWNER)
    )
    assert resp.status_code == 200
    assert resp.get_json()["download_url"].startswith("https://s3.local/get/mail/att/")


def test_suspicious_attachment_is_403(app):
    res = _ingest(OWNER, "Harshith Manne", OWNER_ADDR, "mismatch_attachment.eml", "<mm-1@corp>")
    aid = _first_attachment_id(app, OWNER, res["thread_id"])
    resp = app.test_client().get(
        f"/api/resume/mailbox/attachments/{aid}", headers=_hdr(app, OWNER)
    )
    assert resp.status_code == 403
    assert "download_url" not in resp.get_json()


def test_blocked_attachment_is_403(app):
    res = _ingest(OWNER, "Harshith Manne", OWNER_ADDR, "zipbomb.eml", "<zb-1@corp>")
    aid = _first_attachment_id(app, OWNER, res["thread_id"])
    resp = app.test_client().get(
        f"/api/resume/mailbox/attachments/{aid}", headers=_hdr(app, OWNER)
    )
    assert resp.status_code == 403


def test_foreign_attachment_is_404(app):
    res = _ingest(OTHER, "Other Person", OTHER_ADDR, "pdf_attachment.eml", "<x-1@corp>")
    aid = _first_attachment_id(app, OTHER, res["thread_id"])
    mailbox.provision(OWNER, "Harshith Manne")
    resp = app.test_client().get(
        f"/api/resume/mailbox/attachments/{aid}", headers=_hdr(app, OWNER)
    )
    assert resp.status_code == 404
    assert resp.status_code != 401


# ---------------------------------------------------------------------------
# attach / detach — validates application ownership
# ---------------------------------------------------------------------------

def test_attach_validates_application_ownership(app):
    res = _ingest(OWNER, "Harshith Manne", OWNER_ADDR, "pdf_attachment.eml", "<o-1@corp>")
    _seed_application(OWNER, "rec-owner-1")
    client = app.test_client()
    resp = client.post(
        f"/api/resume/mailbox/threads/{res['thread_id']}/attach",
        json={"application_id": "rec-owner-1"}, headers=_hdr(app, OWNER),
    )
    assert resp.status_code == 200 and resp.get_json()["ok"] is True
    assert _db().mail_threads.find_one({"thread_id": res["thread_id"]})["application_id"] == "rec-owner-1"


def test_attach_foreign_application_is_404(app):
    res = _ingest(OWNER, "Harshith Manne", OWNER_ADDR, "pdf_attachment.eml", "<o-1@corp>")
    _seed_application(OTHER, "rec-other-1")     # belongs to a DIFFERENT user
    resp = app.test_client().post(
        f"/api/resume/mailbox/threads/{res['thread_id']}/attach",
        json={"application_id": "rec-other-1"}, headers=_hdr(app, OWNER),
    )
    assert resp.status_code == 404
    assert resp.status_code != 401


def test_attach_missing_application_id_is_400(app):
    res = _ingest(OWNER, "Harshith Manne", OWNER_ADDR, "pdf_attachment.eml", "<o-1@corp>")
    resp = app.test_client().post(
        f"/api/resume/mailbox/threads/{res['thread_id']}/attach",
        json={}, headers=_hdr(app, OWNER),
    )
    assert resp.status_code == 400
    assert resp.status_code != 401


def test_detach_clears_application(app):
    res = _ingest(OWNER, "Harshith Manne", OWNER_ADDR, "pdf_attachment.eml", "<o-1@corp>")
    _seed_application(OWNER, "rec-owner-1")
    client = app.test_client()
    client.post(f"/api/resume/mailbox/threads/{res['thread_id']}/attach",
                json={"application_id": "rec-owner-1"}, headers=_hdr(app, OWNER))
    resp = client.post(f"/api/resume/mailbox/threads/{res['thread_id']}/detach",
                       headers=_hdr(app, OWNER))
    assert resp.status_code == 200 and resp.get_json()["ok"] is True
    assert _db().mail_threads.find_one({"thread_id": res["thread_id"]})["application_id"] is None


# ---------------------------------------------------------------------------
# read — toggles unread_count
# ---------------------------------------------------------------------------

def test_read_toggles_unread_count(app):
    res = _ingest(OWNER, "Harshith Manne", OWNER_ADDR, "pdf_attachment.eml", "<o-1@corp>")
    client = app.test_client()

    # Fresh message -> unread_count 1 on the mailbox surface.
    before = client.get("/api/resume/mailbox", headers=_hdr(app, OWNER)).get_json()
    assert before["unread_count"] == 1

    resp = client.post(f"/api/resume/mailbox/threads/{res['thread_id']}/read",
                       json={"read": True}, headers=_hdr(app, OWNER))
    assert resp.status_code == 200 and resp.get_json()["ok"] is True

    after = client.get("/api/resume/mailbox", headers=_hdr(app, OWNER)).get_json()
    assert after["unread_count"] == 0
    detail = client.get(f"/api/resume/mailbox/threads/{res['thread_id']}",
                        headers=_hdr(app, OWNER)).get_json()
    assert detail["messages"][0]["read"] is True


def test_read_foreign_thread_is_404(app):
    other = _ingest(OTHER, "Other Person", OTHER_ADDR, "pdf_attachment.eml", "<x-1@corp>")
    mailbox.provision(OWNER, "Harshith Manne")
    resp = app.test_client().post(
        f"/api/resume/mailbox/threads/{other['thread_id']}/read",
        json={"read": True}, headers=_hdr(app, OWNER),
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# report-phishing — flips trust + flag
# ---------------------------------------------------------------------------

def test_report_phishing_flips_trust_and_flag(app):
    _seed_application(OWNER, "rec-owner-1", domain="greenhouse.io")
    res = _ingest(OWNER, "Harshith Manne", OWNER_ADDR, "rejection_greenhouse.eml",
                  "<rej-1@greenhouse.io>", frm="no-reply@greenhouse.io")
    # Correlated -> the thread starts verified.
    assert _db().mail_threads.find_one({"thread_id": res["thread_id"]})["trust"] == "verified"

    resp = app.test_client().post(
        f"/api/resume/mailbox/threads/{res['thread_id']}/report-phishing",
        headers=_hdr(app, OWNER),
    )
    assert resp.status_code == 200 and resp.get_json()["ok"] is True

    thread = _db().mail_threads.find_one({"thread_id": res["thread_id"]})
    assert thread["trust"] == "unverified"          # dropped
    msg = _db().mail_messages.find_one({"message_id": res["message_id"]})
    assert msg["is_phishing_reported"] is True


# ---------------------------------------------------------------------------
# delete thread — owner-scoped hard delete of thread + messages + attachments
# ---------------------------------------------------------------------------

def test_delete_thread_removes_thread_messages_attachments(app, fake_s3):
    res = _ingest(OWNER, "Harshith Manne", OWNER_ADDR, "pdf_attachment.eml", "<o-1@corp>")
    tid, mid = res["thread_id"], res["message_id"]
    assert _db().mail_messages.count_documents({"thread_id": tid}) == 1
    assert _db().mail_attachments.count_documents({"message_id": mid}) >= 1
    # Bodies exist in S3 before the delete.
    assert fake_s3.objects, "expected stored S3 objects before delete"

    resp = app.test_client().delete(
        f"/api/resume/mailbox/threads/{tid}", headers=_hdr(app, OWNER)
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert body["deleted"]["messages"] == 1
    assert body["deleted"]["attachments"] >= 1

    # Everything is gone, owner-scoped.
    assert _db().mail_threads.count_documents({"thread_id": tid}) == 0
    assert _db().mail_messages.count_documents({"thread_id": tid}) == 0
    assert _db().mail_attachments.count_documents({"message_id": mid}) == 0


def test_delete_foreign_thread_is_404_and_deletes_nothing(app):
    other = _ingest(OTHER, "Other Person", OTHER_ADDR, "pdf_attachment.eml", "<x-1@corp>")
    mailbox.provision(OWNER, "Harshith Manne")
    resp = app.test_client().delete(
        f"/api/resume/mailbox/threads/{other['thread_id']}", headers=_hdr(app, OWNER)
    )
    assert resp.status_code == 404
    assert resp.status_code != 401
    # OTHER's thread + message untouched.
    assert _db().mail_threads.count_documents({"thread_id": other["thread_id"]}) == 1
    assert _db().mail_messages.count_documents({"thread_id": other["thread_id"]}) == 1


# ---------------------------------------------------------------------------
# reply — deferred (501, never 401)
# ---------------------------------------------------------------------------

def test_reply_is_deferred_501(app):
    res = _ingest(OWNER, "Harshith Manne", OWNER_ADDR, "pdf_attachment.eml", "<o-1@corp>")
    resp = app.test_client().post(
        f"/api/resume/mailbox/threads/{res['thread_id']}/reply",
        json={"body": "hi"}, headers=_hdr(app, OWNER),
    )
    assert resp.status_code == 501
    assert resp.status_code != 401


# ---------------------------------------------------------------------------
# Auth: no JWT -> 401 (from @jwt_required only), on EVERY endpoint
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("method,path", [
    ("get", "/api/resume/mailbox"),
    ("post", "/api/resume/mailbox/provision"),
    ("post", "/api/resume/mailbox/pause"),
    ("post", "/api/resume/mailbox/rotate"),
    ("get", "/api/resume/mailbox/threads"),
    ("get", "/api/resume/mailbox/threads/thr_x"),
    ("post", "/api/resume/mailbox/threads/thr_x/read"),
    ("post", "/api/resume/mailbox/threads/thr_x/attach"),
    ("post", "/api/resume/mailbox/threads/thr_x/detach"),
    ("post", "/api/resume/mailbox/threads/thr_x/report-phishing"),
    ("delete", "/api/resume/mailbox/threads/thr_x"),
    ("get", "/api/resume/mailbox/attachments/att_x"),
])
def test_endpoints_require_jwt(app, method, path):
    resp = getattr(app.test_client(), method)(path)
    assert resp.status_code == 401     # missing token — the ONLY 401 allowed


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))

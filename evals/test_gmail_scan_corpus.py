"""The Gmail scan, held to a labelled mailbox.

Runs every email in `gmail_scan_corpus` through the real pipeline: Gmail's
message JSON -> text extraction -> matching -> rules -> tracker and review
queue. Offline: mongomock, no Google, no model.

Gates, in the order they matter to a person using the tracker:
  1. Nothing is ever moved to the wrong status.           (zero tolerance)
  2. Mail that is not about an application writes nothing. (zero tolerance)
  3. A clear outcome is applied without asking.
  4. An unclear one is asked about, with the right proposal.
  5. The search Gmail is asked to run would have returned every email the
     rules can use.
"""
import base64
import re
from datetime import datetime, timedelta, timezone

import pytest

from _vg_support import current_db
from evals import gmail_scan_corpus as corpus
from services import gmail_service as gs

USER = "corpus@example.test"
NOW = datetime.now(timezone.utc)
BASE_MS = int((NOW - timedelta(days=3)).timestamp() * 1000)


def _gmail_json(item):
    """One email the way the Gmail API returns it with format=full."""
    def part(mime, text, charset):
        raw = text.encode(charset, errors="replace")
        return {"mimeType": mime,
                "headers": [{"name": "Content-Type", "value": f'{mime}; charset="{charset}"'}],
                "body": {"data": base64.urlsafe_b64encode(raw).decode()}}

    headers = [{"name": "From", "value": item["sender"]}, {"name": "Subject", "value": item["subject"]},
               {"name": "Date", "value": "Mon, 2 Nov 2026 10:00:00 -0800"}]
    if item["html"]:
        parts = [part("text/html", item["body"], item["charset"])]
        if item["plain_stub"]:
            parts.insert(0, part("text/plain", item["plain_stub"], "utf-8"))
        payload = {"mimeType": "multipart/alternative", "headers": headers, "parts": parts}
    else:
        payload = {**part("text/plain", item["body"], item["charset"]), "headers": headers}
    visible = re.sub(r"<[^>]+>", " ", item["body"])
    return {"id": item["id"], "threadId": f"t-{item['id']}", "labelIds": item["labels"],
            "internalDate": str(BASE_MS + item["when"] * 3_600_000),
            "snippet": " ".join(visible.split())[:160], "payload": payload}


@pytest.fixture
def scan(monkeypatch):
    """Returns run(emails) -> (statuses by record, pending review rows, summary)."""
    db = current_db()

    def run(emails, before=None):
        for name in ("gmail_connections", "gmail_suggestions", "tailoring_records", "applications"):
            db[name].delete_many({})
        db.gmail_connections.insert_one({"user_email": USER, "gmail_address": corpus.ME,
                                         "refresh_token_enc": "x", "linked_at": NOW})
        for record_id, (company, title, status) in corpus.TRACKER.items():
            db.tailoring_records.insert_one({
                "user_email": USER, "record_id": record_id, "created_at": NOW - timedelta(days=20),
                "jd_analysis": {"company": company, "job_title": title},
                "application": {"status": status}})
        if before:
            before(db)
        by_id = {item["id"]: _gmail_json(item) for item in emails}

        def gmail(path, access, params=None, raise_on_error=False, **_):
            if path.endswith("/messages"):
                return {"messages": [{"id": mid} for mid in by_id]}
            return by_id[path.rsplit("/", 1)[-1]]

        monkeypatch.setattr(gs, "_get_access_token", lambda user: ("access", gs.get_connection(user)))
        monkeypatch.setattr(gs, "_gmail_get", gmail)
        monkeypatch.setattr(gs, "classify_record_thread",
                            lambda *_: pytest.fail("the scan must not spend on a model by default"))
        summary = gs.sync_user(USER)
        statuses = {r["record_id"]: r["application"]["status"] for r in db.tailoring_records.find()}
        return statuses, gs.list_suggestions(USER), summary

    return run


def _moved(statuses):
    return {rid: status for rid, status in statuses.items() if status != corpus.TRACKER[rid][2]}


def _check(expect, statuses, pending):
    """Return a sentence describing what went wrong, or None."""
    moved = _moved(statuses)
    kind = expect[0]
    if kind == "auto":
        _, record_id, status = expect
        if corpus.TRACKER[record_id][2] == status:
            return None if not moved and not pending else f"expected no change, got moved={moved} pending={len(pending)}"
        if moved != {record_id: status}:
            return f"expected {record_id} -> {status} by itself, got moved={moved}, pending={_brief(pending)}"
        return None
    if moved:
        return f"nothing may move by itself here, but {moved} did"
    if kind == "ignore":
        return f"expected nothing written, got {_brief(pending)}" if pending else None
    if kind == "not":
        bad = [row for row in pending if row.get("suggested_status") == expect[1]]
        return f"must never propose {expect[1]}, got {_brief(bad)}" if bad else None
    if kind == "unmatched":
        if len(pending) != 1 or not pending[0].get("requires_application_match"):
            return f"expected one 'no matching application' row, got {_brief(pending)}"
        return None
    if kind == "review":
        _, target, status = expect
        wanted = target if isinstance(target, set) else {target}
        if len(pending) != 1:
            return f"expected one review row, got {_brief(pending)}"
        row = pending[0]
        offered = {c["record_id"] for c in row.get("candidate_records") or []} or {row.get("record_id")}
        if offered != wanted or row.get("suggested_status") != status:
            return f"expected review of {sorted(wanted)} as {status}, got {_brief(pending)}"
        return None
    raise AssertionError(f"unknown expectation {expect!r}")


def _brief(rows):
    return [(row.get("record_id"), row.get("suggested_status"),
             sorted(c["record_id"] for c in row.get("candidate_records") or []),
             (row.get("reason") or "")[:70]) for row in rows]


@pytest.mark.parametrize("item", corpus.EMAILS, ids=[f"{e['id']}:{e['note']}" for e in corpus.EMAILS])
def test_each_email_is_handled_as_labelled(scan, item):
    statuses, pending, _ = scan([item])
    problem = _check(item["expect"], statuses, pending)
    assert problem is None, f"{item['id']} ({item['note']}): {problem}"


@pytest.mark.parametrize("thread", corpus.THREADS, ids=[f"{t['id']}:{t['note']}" for t in corpus.THREADS])
def test_several_emails_about_one_application(scan, thread):
    statuses, pending, _ = scan(thread["emails"])
    problem = _check(thread["expect"], statuses, pending)
    assert problem is None, f"{thread['id']} ({thread['note']}): {problem}"


def test_the_whole_mailbox_at_once_moves_nothing_wrongly(scan):
    """All of it in one scan, the way an inbox really arrives. Later emails may
    override earlier ones, so only the two hard guarantees are checked."""
    statuses, pending, summary = scan(corpus.EMAILS)
    allowed = {}
    for item in corpus.EMAILS:
        if item["expect"][0] == "auto":
            allowed.setdefault(item["expect"][1], set()).add(item["expect"][2])
    for record_id, status in _moved(statuses).items():
        assert status in allowed.get(record_id, set()), (
            f"{record_id} was moved to {status}, which no email in the mailbox supports")
    noise = {item["id"] for item in corpus.EMAILS if item["expect"][0] == "ignore"}
    leaked = [row for row in pending if row.get("gmail_message_id") in noise]
    assert not leaked, f"mail that is not about an application reached the review queue: {_brief(leaked)}"
    assert summary["skipped_own"] == 1, "the person's own sent mail must be left out and counted"


def _gmail_would_find(item):
    """A plain reading of Gmail's search: a term matches a whole word or phrase."""
    text = " ".join(re.sub(r"<[^>]+>", " ", f"{item['subject']} {item['body']}").lower().split())
    for term in gs._SEARCH_TERMS:
        if re.search(r"(?<![a-z0-9])" + re.escape(term.strip('"').lower()) + r"(?![a-z0-9])", text):
            return True
    return False


def test_the_search_returns_every_email_the_rules_can_use():
    usable = [item for item in corpus.EMAILS if item["expect"][0] in ("auto", "review", "unmatched")]
    usable += [email for thread in corpus.THREADS for email in thread["emails"]]
    missed = [item["id"] for item in usable if not _gmail_would_find(item)]
    assert not missed, f"the Gmail search would never return these emails: {missed}"


def test_the_search_asks_only_for_application_mail_and_never_for_sent_mail(monkeypatch):
    query = gs._search_query(1_700_000_000, 1_700_100_000, ["jane.park@acme.test", "not an address"])
    assert "-in:sent" in query and "-category:promotions" in query
    assert "{" in query and "interview" in query and "from:jane.park@acme.test" in query
    assert "not an address" not in query
    monkeypatch.setattr(gs, "_get_config_value", lambda key, default="": "all" if key == "GMAIL_SCAN_SCOPE" else default)
    assert "{" not in gs._search_query(1_700_000_000, 1_700_100_000)


def test_a_connection_scanned_under_older_rules_reads_its_window_again_once(scan, monkeypatch):
    statuses, _, _ = scan([corpus.EMAILS[0]])
    db = current_db()
    assert db.gmail_connections.find_one()["scan_version"] == gs.SCAN_VERSION
    seen = []
    monkeypatch.setattr(gs, "fetch_recent_messages", lambda _, since, *__: seen.append(since) or [])
    gs.sync_user(USER)
    assert seen[-1] is not None, "already on the current rules: an ordinary top-up"
    db.gmail_connections.update_one({}, {"$set": {"scan_version": gs.SCAN_VERSION - 1}})
    gs.sync_user(USER)
    assert seen[-1] is None, "older rules: the whole window is read again"
    assert db.gmail_connections.find_one()["scan_version"] == gs.SCAN_VERSION
    gs.sync_user(USER)
    assert seen[-1] is not None, "and only once"


def test_review_queue_cost_does_not_grow_with_the_size_of_the_tracker():
    """420 applications and 300 waiting emails once took 25 seconds per request
    and timed the tracker out. The work per email must not depend on how many
    applications are tracked."""
    import time

    def cost(records, rows):
        index = gs._build_record_index([
            {"record_id": f"r{i}", "jd_analysis": {"company": f"Company{i} Labs",
                                                   "job_title": f"Engineer Level {i} - Team {i}"},
             "application": {"status": "applied"}} for i in range(records)])
        pending = [{"suggestion_id": f"s{i}", "subject": f"Update from Company{i} Labs about your application",
                    "snippet": "Your application has an update in the candidate portal.",
                    "record_id": f"r{i}", "candidate_records": [{"record_id": f"r{i}"}],
                    "applied": False, "dismissed": False} for i in range(rows)]
        started = time.perf_counter()
        kept = [gs._validated_suggestion(dict(row), index) for row in pending]
        assert all(kept)
        return (time.perf_counter() - started) / rows

    small, large = cost(40, 40), cost(600, 150)
    assert large < 0.02, f"{large * 1000:.1f} ms per waiting email with 600 applications"
    assert large < small * 8 + 0.002, "cost per email grew with the tracker"


def _old_interview_move(at, touched=None, message="F1"):
    """The card was moved to interviewing by itself off email F1 under the old
    rules; `touched` is when the person last changed it, if after."""
    def before(db):
        db.tailoring_records.update_one({"record_id": "vandelay-backend"}, {"$set": {
            "application.status": "interviewing", "application.updated_at": touched or at,
            "application.gmail_updated_at": at}})
        db.gmail_suggestions.insert_one({
            "_id": "old-move", "suggestion_id": "old-move", "user_email": USER,
            "record_id": "vandelay-backend", "gmail_message_id": message, "evidence_message_id": message,
            "supporting_message_ids": [message], "current_status": "applied",
            "suggested_status": "interviewing", "auto_applied": True, "applied": True,
            "applied_at": at, "dismissed": False, "created_at": at})
    return before


def test_an_automatic_interview_move_the_rules_no_longer_support_is_put_back(scan):
    f1 = next(e for e in corpus.EMAILS if e["id"] == "F1")
    statuses, _, _ = scan([f1], before=_old_interview_move(NOW - timedelta(days=1)))
    assert statuses["vandelay-backend"] == "applied"
    row = current_db().gmail_suggestions.find_one({"_id": "old-move"})
    assert row["undone"] is True and row["retracted_by_rules"] is True
    activity = {a["suggestion_id"]: a for a in gs.list_activity(USER)}
    assert activity["old-move"]["undone"] and activity["old-move"]["corrected"], \
        "the person sees it was put back by a closer read, not by them"


def test_a_card_the_person_changed_since_is_left_alone(scan):
    f1 = next(e for e in corpus.EMAILS if e["id"] == "F1")
    at = NOW - timedelta(days=2)
    statuses, _, _ = scan([f1], before=_old_interview_move(at, touched=at + timedelta(days=1)))
    assert statuses["vandelay-backend"] == "interviewing"
    assert not current_db().gmail_suggestions.find_one({"_id": "old-move"}).get("undone")


def test_a_move_made_from_a_receipt_by_its_subject_is_put_back(scan):
    """The Brown University Health case (2026-11-16): 'Application Received for
    Cloud Engineer II' moved the card to Interviewing on 'we would like to
    schedule an interview'."""
    f6 = next(e for e in corpus.EMAILS if e["id"] == "F6")
    statuses, _, _ = scan([f6], before=_old_interview_move(NOW - timedelta(days=1), message="F6"))
    assert statuses["vandelay-backend"] == "applied"
    assert current_db().gmail_suggestions.find_one({"_id": "old-move"})["retracted_by_rules"] is True

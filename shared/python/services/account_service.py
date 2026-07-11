"""Account erasure service — GDPR Art. 17 ("right to be forgotten") / CCPA.

Closes AUDIT-P1-004 / PRIV-1: JobPilot previously had NO account-deletion path.

Two entry points, wired into the two-phase deletion flow:

  * ``soft_delete(email)`` — flips a deletion marker on the ``users`` doc
    SYNCHRONOUSLY so the account can no longer be used, clears any pending
    password-reset tokens, and returns immediately. The route runs this first
    (so the user is logged out at once) and then enqueues the async purge.

  * ``hard_purge(email)`` — the heavy cascade. Runs as the ``account_purge``
    async job (touches ~50 collections + S3 + three OAuth providers, well past
    API Gateway's 29s budget). Deletes every user-keyed document across ALL
    discovered collections, purges the user's S3 objects, and best-effort
    revokes provider OAuth tokens (Gmail / GitHub / LinkedIn).

Design contract:
  * IDEMPOTENT — every step is a delete or a best-effort revoke, so re-running
    the job (Lambda async is at-least-once) converges to the same empty state.
  * FAIL-SOFT PER STORE — one collection / S3 / provider failing must NEVER
    abort the rest; each failure is caught, logged (redacted), and collected
    into the returned ``errors`` list so the cascade always runs to completion.
  * REDACTED AUDIT TRAIL — the email is masked in every log line; no raw PII.

The ``users`` document is deleted LAST, after the S3 objects, because S3 keys
are prefixed with the Mongo ``_id`` (``{user_id}/base/...``), not the email —
so we must resolve ``_id`` from the still-present users doc before removing it.

Coupling note: this is a shared-layer service, so it must not import a
domain-local service at module scope. The GitHub/LinkedIn ``disconnect`` helpers
live in the resume Lambda's local ``services/`` and are imported LAZILY inside
``_revoke_providers`` (guarded by try/except) — when the purge runs in the
resume Lambda they resolve; anywhere else the ImportError is swallowed and the
provider's token doc is still removed by the generic cascade below.
"""
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from utils.db_connect import DBConnect

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _redact(email: str) -> str:
    """Mask an email for logs: ``harshith@aithrex.tech`` -> ``h***@a***``."""
    if not email or "@" not in email:
        return "***"
    local, _, domain = email.partition("@")
    return f"{local[:1]}***@{domain[:1]}***"


# ---------------------------------------------------------------------------
# The definitive cascade.
#
# Every collection keyed by the account holder's email, with the exact field
# name that holds it. Discovered by grepping every insert/update/upsert across
# shared/python/services and each services/<svc>/services (AUDIT-P1-004).
#
# `users` is handled SEPARATELY (deleted last, after S3). Deliberately EXCLUDED
# — see module docstring / docs/product/data-deletion.md:
#   * global caches (not user-keyed): jobs_cache, apify_actor_cache,
#     sponsorship_signals, salary_benchmarks, events, scrape_runs, bandit_arms,
#     bandit_updates, stripe_events, tech_chronicle_cache, career_pages_cache,
#     workday_jobs_cache
#   * audit_log / eval_ratings — internal admin/rater trails, not keyed by the
#     account holder's email; retained.
#   * billing_incidents — financial record, retained under a legal-basis
#     exception (tax/chargeback), not erased by default.
#   * oauth_login_states — ephemeral (10-min TTL), keyed by random `state` and
#     holds no email, so nothing to target.
# ---------------------------------------------------------------------------
CASCADE_COLLECTIONS: List[Tuple[str, str]] = [
    # --- identity / preferences / resumes ---
    ("user_preferences", "user_email"),
    ("user_resumes", "user_email"),
    ("password_reset_tokens", "user_email"),
    ("tailoring_records", "user_email"),
    ("feedback", "email"),
    # --- usage / entitlements ---
    ("tailor_usage", "email"),
    ("feature_usage", "user_email"),
    ("entitlement_overrides", "user_email"),
    # --- Voice Graph (all user_email-scoped) ---
    ("graph_nodes", "user_email"),
    ("graph_edges", "user_email"),
    ("graph_evidence", "user_email"),
    ("writing_samples", "user_email"),
    ("style_profiles", "user_email"),
    ("authored_resumes", "user_email"),
    ("consents", "user_email"),
    ("merge_candidates", "user_email"),
    # --- jobs / apply / extension ---
    ("saved_jobs", "user_email"),
    ("application_answers", "user_email"),
    ("application_profiles", "user_email"),
    # --- auto-apply Tranche 1 (ledger / approval loop / credential vault) ---
    ("applications", "user_email"),
    ("approval_tokens", "user_email"),
    ("ats_credentials", "user_email"),
    ("ats_credential_audit", "user_email"),
    # --- auto-apply Tranche 2/3 (answers cache, verify freezes, answer library);
    #     skill_market_stats is cross-user aggregate data, NOT user-keyed — excluded ---
    ("auto_apply_answers", "user_email"),
    ("auto_apply_freezes", "user_email"),
    ("application_answer_library", "user_email"),
    # --- opt-in auto-submit (were missing — user-keyed, must be erased too) ---
    ("auto_submit_settings", "user_email"),
    ("auto_submit_approvals", "user_email"),
    ("auto_submit_audit", "user_email"),
    ("job_activity_log", "user_email"),
    ("job_filter_preferences", "user_email"),
    ("pipeline_filter_presets", "user_email"),
    ("user_apify_keys", "user_email"),
    ("user_events", "user_email"),
    # --- career intelligence ---
    ("networking_contacts", "user_email"),
    ("offer_workspace", "user_email"),
    ("differentiation_analyses", "user_email"),
    ("github_profiles", "user_email"),
    ("visa_profiles", "user_email"),
    ("career_copilot_state", "user_email"),
    ("career_copilot_outreach", "user_email"),
    ("interview_attempts", "user_email"),
    # --- learning / posting ---
    ("learning_paths", "user_email"),
    ("learning_progress", "user_email"),
    ("post_drafts", "user_email"),
    ("post_metrics", "user_email"),
    ("scheduled_posts", "user_email"),
    ("linkedin_archives", "user_email"),
    # --- integrations / provider token docs ---
    ("gmail_connections", "user_email"),
    ("gmail_suggestions", "user_email"),
    ("gmail_oauth_states", "user_email"),
    ("github_connections", "user_email"),
    ("linkedin_connections", "user_email"),
    ("connect_oauth_states", "user_email"),
    # --- mail ingress ---
    ("mail_ingress_addresses", "user_email"),
    ("email_ingress", "user_email"),
    ("mail_ingress_suggestions", "user_email"),
    # --- notifications ---
    ("notification_prefs", "user_email"),
    ("notification_log", "user_email"),
    ("notification_consents", "user_email"),
    # --- async job store (excluding the running purge job — see hard_purge) ---
    ("resume_jobs", "user_email"),
]


# ---------------------------------------------------------------------------
# Phase 1 — synchronous soft delete
# ---------------------------------------------------------------------------
def soft_delete(email: str) -> Dict[str, Any]:
    """Mark the account deleted and neutralize it synchronously.

    Flips ``is_deleted``/``deleted_at`` on the users doc (login should reject a
    flagged account) and drops any outstanding password-reset tokens so no
    dangling reset link can resurrect it. JWTs are stateless — there is no
    server-side session store to revoke — so the frontend performs the actual
    logout; this flag is the durable server-side signal.

    Idempotent: safe to call on an already-flagged (or already-purged) account.
    """
    db = DBConnect().get_db()
    now = _now()
    existed = False
    try:
        res = db.users.update_one(
            {"email": email},
            {"$set": {
                "is_deleted": True,
                "deleted_at": now,
                "deletion_status": "pending_purge",
                "status": "deleted",
            }},
        )
        existed = res.matched_count > 0
    except Exception as e:
        logger.error("soft_delete: users flag failed for %s: %s", _redact(email), e)

    # Kill any pending reset tokens so the account can't be revived via reset.
    try:
        db.password_reset_tokens.delete_many({"user_email": email})
    except Exception as e:
        logger.warning("soft_delete: reset-token purge failed for %s: %s", _redact(email), e)

    logger.info("soft_delete complete for %s (existed=%s)", _redact(email), existed)
    return {"soft_deleted": True, "existed": existed, "deleted_at": now.isoformat()}


# ---------------------------------------------------------------------------
# Phase 2 — asynchronous hard purge
# ---------------------------------------------------------------------------
def hard_purge(email: str, exclude_job_id: Optional[str] = None) -> Dict[str, Any]:
    """Irreversibly erase every trace of ``email`` across Mongo, S3, providers.

    Args:
        email: the account holder's email (JWT identity).
        exclude_job_id: the ``resume_jobs`` doc id of the running purge job, so
            the cascade doesn't delete the very row the poller is watching.

    Returns a redaction-safe report: per-collection deleted counts, s3 object
    count, provider revocation outcomes, and a collected ``errors`` list. Never
    raises for a per-store failure — the whole cascade always runs.
    """
    db = DBConnect().get_db()
    report: Dict[str, Any] = {
        "email": _redact(email),
        "deleted": {},
        "s3_objects_deleted": 0,
        "providers": {},
        "errors": [],
    }

    # Resolve the S3 key prefix BEFORE deleting the users doc (keys use _id).
    user_id: Optional[str] = None
    try:
        user = db.users.find_one({"email": email}, {"_id": 1})
        if user:
            user_id = str(user["_id"])
    except Exception as e:
        report["errors"].append(f"resolve_user_id: {e}")
        logger.error("hard_purge: user_id resolve failed for %s: %s", _redact(email), e)

    # 1. Best-effort revoke provider OAuth tokens at the source (also deletes
    #    their own docs; the generic cascade mops up any remainder).
    report["providers"] = _revoke_providers(email)

    # 2. Generic Mongo cascade — one delete_many per keyed collection.
    for coll, key in CASCADE_COLLECTIONS:
        try:
            flt: Dict[str, Any] = {key: email}
            if coll == "resume_jobs" and exclude_job_id:
                flt["job_id"] = {"$ne": exclude_job_id}
            res = db[coll].delete_many(flt)
            if res.deleted_count:
                report["deleted"][coll] = res.deleted_count
        except Exception as e:
            report["errors"].append(f"{coll}: {e}")
            logger.warning("hard_purge: %s delete failed for %s: %s", coll, _redact(email), e)

    # 3. Purge the user's S3 objects ({user_id}/base/... and /generated/...).
    if user_id:
        try:
            report["s3_objects_deleted"] = _purge_s3_objects(user_id)
        except Exception as e:
            report["errors"].append(f"s3: {e}")
            logger.error("hard_purge: S3 purge failed for %s: %s", _redact(email), e)

    # 4. Remove the identity record LAST.
    try:
        res = db.users.delete_one({"email": email})
        if res.deleted_count:
            report["deleted"]["users"] = res.deleted_count
    except Exception as e:
        report["errors"].append(f"users: {e}")
        logger.error("hard_purge: users delete failed for %s: %s", _redact(email), e)

    logger.info(
        "hard_purge complete for %s: %d collections cleared, %d s3 objects, %d errors",
        _redact(email), len(report["deleted"]), report["s3_objects_deleted"],
        len(report["errors"]),
    )
    return report


# ---------------------------------------------------------------------------
# Best-effort provider revocation (isolated so tests can monkeypatch it).
# ---------------------------------------------------------------------------
def _revoke_providers(email: str) -> Dict[str, Any]:
    """Revoke Gmail / GitHub / LinkedIn OAuth grants. Fail-soft per provider.

    Each provider's ``disconnect`` both revokes the token at the provider and
    deletes the local connection doc. GitHub/LinkedIn live in the resume
    Lambda's local services, imported lazily; if unavailable (wrong Lambda /
    offline test) the ImportError is swallowed and the generic cascade still
    deletes the connection docs.
    """
    out: Dict[str, Any] = {}

    # Gmail lives in the shared layer — always importable.
    try:
        from services.gmail_service import disconnect as gmail_disconnect
        out["gmail"] = bool(gmail_disconnect(email))
    except Exception as e:
        out["gmail"] = f"error: {e}"
        logger.warning("revoke gmail failed for %s: %s", _redact(email), e)

    for name, module in (("github", "services.github_connect_service"),
                         ("linkedin", "services.linkedin_connect_service")):
        try:
            import importlib
            mod = importlib.import_module(module)
            out[name] = bool(mod.disconnect(email))
        except ImportError:
            out[name] = "unavailable"  # not on this Lambda's path; docs cascaded
        except Exception as e:
            out[name] = f"error: {e}"
            logger.warning("revoke %s failed for %s: %s", name, _redact(email), e)

    return out


# ---------------------------------------------------------------------------
# S3 purge (isolated so tests can monkeypatch it — keeps the suite offline).
# ---------------------------------------------------------------------------
def _purge_s3_objects(user_id: str) -> int:
    """Delete every S3 object under the ``{user_id}/`` prefix. Returns count."""
    from services.s3_service import get_storage_service

    storage = get_storage_service()
    bucket = storage.bucket
    if not bucket:
        logger.warning("S3 purge skipped: RESUME_S3_BUCKET unset")
        return 0

    client = storage.client
    prefix = f"{user_id}/"
    deleted = 0
    token: Optional[str] = None
    while True:
        kwargs = {"Bucket": bucket, "Prefix": prefix}
        if token:
            kwargs["ContinuationToken"] = token
        resp = client.list_objects_v2(**kwargs)
        keys = [{"Key": o["Key"]} for o in resp.get("Contents", [])]
        if keys:
            client.delete_objects(Bucket=bucket, Delete={"Objects": keys})
            deleted += len(keys)
        if resp.get("IsTruncated"):
            token = resp.get("NextContinuationToken")
        else:
            break
    return deleted

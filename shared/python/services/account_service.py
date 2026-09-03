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
    ("resume_assets", "user_email"),          # v3 canonical resume store
    ("password_reset_tokens", "user_email"),
    ("tailoring_records", "user_email"),
    ("feedback", "email"),
    # --- usage / entitlements / quota ledger (v3 §1.6) ---
    ("tailor_usage", "email"),
    ("feature_usage", "user_email"),
    ("entitlement_overrides", "user_email"),
    ("quota_ledger", "user_email"),
    # guest day-1 carry-in markers are keyed by the claiming account's email
    ("guest_claim_carryins", "email"),
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
    ("credential_reveal_challenges", "user_email"),
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

    # Collect the user's resume_assets ids BEFORE the cascade deletes the
    # docs — their S3 objects live under resume-assets/{resume_id}/ prefixes
    # (Resume Platform v3 spec section 2) and must be version-purged too.
    asset_resume_ids: List[str] = []
    try:
        owner_or: List[Dict[str, Any]] = [{"user_email": email}]
        if user_id:
            owner_or.append({"user_id": user_id})
        for a in db.resume_assets.find({"$or": owner_or}, {"resume_id": 1}):
            rid = str(a.get("resume_id") or "")
            if rid:
                asset_resume_ids.append(rid)
    except Exception as e:
        report["errors"].append(f"resume_assets_collect: {e}")

    # 1. Best-effort revoke provider OAuth tokens at the source (also deletes
    #    their own docs; the generic cascade mops up any remainder).
    report["providers"] = _revoke_providers(email)

    # 1b. Guest data claimed by this account (Resume Platform v3 spec §2):
    #     the claimed guest_sessions + their guest resume_assets docs +
    #     job_match_snapshots + legacy resume_file_s3_keys, the linked-
    #     accounts $pull (subject retained as abuse memory WITHOUT the
    #     email), and quota_counters pool entries where the email is the
    #     pool's sole member.
    guest_s3_keys, guest_asset_ids = _purge_claimed_guest_data(db, email, report)
    asset_resume_ids.extend(rid for rid in guest_asset_ids if rid not in asset_resume_ids)

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

    # 3. Purge the user's S3 objects — VERSIONED deletes (the bucket keeps
    #    noncurrent versions; a plain delete only writes a marker): the
    #    {user_id}/ prefix, every resume-assets/{resume_id}/ prefix the user
    #    owned, and the claimed guest sessions' legacy object keys.
    storage_ok = True
    if user_id:
        try:
            report["s3_objects_deleted"] = _purge_s3_objects(user_id)
        except Exception as e:
            storage_ok = False
            report["errors"].append(f"s3: {e}")
            logger.error("hard_purge: S3 purge failed for %s: %s", _redact(email), e)
    try:
        n, asset_errors = _purge_asset_objects(asset_resume_ids, guest_s3_keys)
        report["s3_objects_deleted"] += n
        if asset_errors:
            storage_ok = False
            report["errors"].extend(f"s3_asset: {e}" for e in asset_errors)
    except Exception as e:
        storage_ok = False
        report["errors"].append(f"s3_assets: {e}")
        logger.error("hard_purge: asset S3 purge failed for %s: %s", _redact(email), e)

    # 4. Remove the identity record LAST — and only after storage cleanup
    #    verified. On S3 failure the users doc is retained (flagged
    #    purge_incomplete) so the idempotent re-run converges; per-store
    #    Mongo failures stay fail-soft as before.
    if storage_ok:
        try:
            res = db.users.delete_one({"email": email})
            if res.deleted_count:
                report["deleted"]["users"] = res.deleted_count
        except Exception as e:
            report["errors"].append(f"users: {e}")
            logger.error("hard_purge: users delete failed for %s: %s", _redact(email), e)
    else:
        try:
            db.users.update_one(
                {"email": email},
                {"$set": {"deletion_status": "purge_incomplete", "updated_at": _now()}},
            )
        except Exception:
            pass
        report["errors"].append("users: retained pending storage cleanup (re-run purge)")
        logger.error(
            "hard_purge: users doc retained for %s — storage cleanup unverified",
            _redact(email),
        )

    logger.info(
        "hard_purge complete for %s: %d collections cleared, %d s3 objects, %d errors",
        _redact(email), len(report["deleted"]), report["s3_objects_deleted"],
        len(report["errors"]),
    )
    return report


# ---------------------------------------------------------------------------
# Claimed-guest-data purge (Resume Platform v3 spec §2 erasure additions).
# ---------------------------------------------------------------------------
def _purge_claimed_guest_data(
    db, email: str, report: Dict[str, Any]
) -> Tuple[List[str], List[str]]:
    """Erase the guest world this account claimed. Fail-soft per store.

    Returns (legacy_s3_keys, guest_asset_resume_ids) for the S3 phase.
    The guest_quota_subjects docs themselves are RETAINED as abuse memory —
    only the account email is $pull-ed from linked_accounts.
    """
    legacy_keys: List[str] = []
    asset_ids: List[str] = []
    guest_ids: List[str] = []
    try:
        sessions = list(db.guest_sessions.find(
            {"$or": [{"claimed_by": email}, {"claim.email": email}]},
            {"guest_id": 1, "resume_file_s3_keys": 1},
        ))
        guest_ids = [str(s["guest_id"]) for s in sessions if s.get("guest_id")]
        for s in sessions:
            legacy_keys.extend(k for k in (s.get("resume_file_s3_keys") or []) if k)
    except Exception as e:
        report["errors"].append(f"guest_sessions_collect: {e}")

    if guest_ids:
        try:
            for a in db.resume_assets.find(
                {"guest_id": {"$in": guest_ids}}, {"resume_id": 1}
            ):
                rid = str(a.get("resume_id") or "")
                if rid:
                    asset_ids.append(rid)
            res = db.resume_assets.delete_many({"guest_id": {"$in": guest_ids}})
            if res.deleted_count:
                report["deleted"]["resume_assets_guest"] = res.deleted_count
        except Exception as e:
            report["errors"].append(f"resume_assets_guest: {e}")
        try:
            res = db.job_match_snapshots.delete_many({"guest_id": {"$in": guest_ids}})
            if res.deleted_count:
                report["deleted"]["job_match_snapshots"] = res.deleted_count
        except Exception as e:
            report["errors"].append(f"job_match_snapshots: {e}")
        try:
            res = db.guest_sessions.delete_many({"guest_id": {"$in": guest_ids}})
            if res.deleted_count:
                report["deleted"]["guest_sessions"] = res.deleted_count
        except Exception as e:
            report["errors"].append(f"guest_sessions: {e}")

    # quota_counters pool entries where the email is the SOLE pool member —
    # computed BEFORE the linked_accounts $pull. Shared pools keep their
    # counters (they still meter the other linked accounts).
    try:
        import re as _re

        subjects = list(db.guest_quota_subjects.find(
            {"linked_accounts": email}, {"linked_accounts": 1}
        ))
        others = {
            e for s in subjects for e in (s.get("linked_accounts") or [])
            if isinstance(e, str) and e and e != email
        }
        if not others:
            res = db.quota_counters.delete_many(
                {"_id": {"$regex": f"^tailor:{_re.escape(email)}:"}}
            )
            if res.deleted_count:
                report["deleted"]["quota_counters"] = res.deleted_count
        db.guest_quota_subjects.update_many(
            {"linked_accounts": email},
            {"$pull": {"linked_accounts": email}},
        )
    except Exception as e:
        report["errors"].append(f"guest_quota_subjects: {e}")

    return legacy_keys, asset_ids


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
    """VERSIONED delete of every S3 object under ``{user_id}/`` (the bucket
    is versioned — a plain delete only writes a marker; erasure must remove
    ALL versions, spec section 2). Returns the deleted count; raises when any
    per-object delete failed so the caller knows storage cleanup is NOT
    verified and retains the users doc for a retry."""
    from services.s3_service import get_storage_service

    storage = get_storage_service()
    if not storage.bucket:
        logger.warning("S3 purge skipped: RESUME_S3_BUCKET unset")
        return 0
    result = storage.delete_prefix_all_versions(f"{user_id}/")
    if result.get("errors"):
        raise RuntimeError(
            f"versioned purge left {len(result['errors'])} object(s) under user prefix"
        )
    return int(result.get("deleted") or 0)


def _purge_asset_objects(
    resume_ids: List[str], legacy_keys: Optional[List[str]] = None
) -> Tuple[int, List[str]]:
    """VERSIONED purge of the user's resume-assets/{resume_id}/ prefixes plus
    any legacy guest object keys. Per-object results inspected; returns
    (deleted_count, errors) — errors mean storage cleanup is unverified."""
    from services.s3_service import get_storage_service

    storage = get_storage_service()
    if not storage.bucket:
        if resume_ids or legacy_keys:
            logger.warning("S3 asset purge skipped: RESUME_S3_BUCKET unset")
        return 0, []
    deleted = 0
    errors: List[str] = []
    prefixes = [f"resume-assets/{rid}/" for rid in (resume_ids or [])]
    # A full key is a valid prefix — legacy guest keys ride the same call.
    prefixes += [str(k) for k in (legacy_keys or []) if k]
    for prefix in prefixes:
        try:
            result = storage.delete_prefix_all_versions(prefix)
            deleted += int(result.get("deleted") or 0)
            errors.extend(result.get("errors") or [])
        except Exception as e:
            errors.append(f"{prefix.split('/', 1)[0]}/...: {e}")
    return deleted, errors

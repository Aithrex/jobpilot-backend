"""Stripe billing "bones" (Compass §6.10) — dark behind BILLING_ENFORCEMENT.

Everything here is a no-op until BOTH:
  1. BILLING_ENFORCEMENT is truthy, AND
  2. real Stripe keys are configured (STRIPE_SECRET_KEY / STRIPE_WEBHOOK_SECRET).

With no keys — or if the `stripe` SDK isn't installed — every entry point returns
a structured "billing not configured" result (HTTP 503) instead of raising, so
the routes and the frontend can be wired NOW without risk in prod.

Design:
  * Plan catalog     — config/plans.yaml (§6.10 free/pro/career) if present, else
                       the entitlements PLAN_META/PLAN_LIMITS matrix. Billing
                       slugs map to canonical entitlements plan ids so that
                       entitlements_service.set_plan() only ever receives a
                       VALID_PLANS value.
  * Idempotency      — webhook events de-duped in `stripe_events` (atomic claim
                       on a unique event_id); checkout/portal pass a Stripe
                       idempotency_key.
  * Signature        — webhooks are verified with Stripe's signing secret; a bad
                       signature is a 400, never processed.
  * Overrides        — `entitlement_overrides` is a permanent per-user admin
                       escape hatch (testing-phase infra): set_override /
                       get_override. entitlements_service should consult it
                       (see the wiring note in the task report).

The `stripe` SDK is imported LAZILY (never at module import) to keep cold starts
lean and to allow the layer to ship without it. HTTP-facing functions return
`(body_dict, status_code)` tuples so the billing blueprint stays trivially thin.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from utils.db_connect import DBConnect
from utils.security import InputSanitizer

logger = logging.getLogger(__name__)

# Plan catalog cache (lazy; loaded on first use, never at import time).
_PLANS_CACHE: Optional[Dict[str, Any]] = None


# ---------------------------------------------------------------------------
# Config / feature flags
# ---------------------------------------------------------------------------
def _cfg(key: str, default: Optional[str] = None) -> Optional[str]:
    from utils.config import _get_config_value
    return _get_config_value(key, default)


def _flag(key: str, default: bool = False) -> bool:
    v = _cfg(key, None)
    if v is None:
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def billing_enabled() -> bool:
    """Master kill-switch. Default OFF — billing stays dark until flipped."""
    return _flag("BILLING_ENFORCEMENT", False)


def _secret_key() -> str:
    return (_cfg("STRIPE_SECRET_KEY", "") or "").strip()


def _webhook_secret() -> str:
    return (_cfg("STRIPE_WEBHOOK_SECRET", "") or "").strip()


def is_configured() -> bool:
    """True only when billing is enabled AND a Stripe secret key is present."""
    return bool(billing_enabled() and _secret_key())


def _db():
    return DBConnect().get_db()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _day_key() -> str:
    return _now().strftime("%Y-%m-%d")


def _not_configured(extra: Optional[Dict[str, Any]] = None) -> Tuple[Dict[str, Any], int]:
    body: Dict[str, Any] = {
        "configured": False,
        "error": "billing_not_configured",
        "message": "Billing is not configured yet.",
    }
    if extra:
        body.update(extra)
    return body, 503


# ---------------------------------------------------------------------------
# Lazy Stripe SDK
# ---------------------------------------------------------------------------
def _stripe():
    """Lazily import + configure the stripe SDK. Returns None if unavailable."""
    try:
        import stripe  # noqa: WPS433 (intentional lazy import)
    except Exception:
        logger.warning("stripe SDK not installed — billing degraded to no-op")
        return None
    sk = _secret_key()
    if not sk:
        return None
    stripe.api_key = sk
    try:
        stripe.max_network_retries = 2
    except Exception:  # pragma: no cover - SDK version guard
        pass
    return stripe


# ---------------------------------------------------------------------------
# Plan catalog (config/plans.yaml → else entitlements matrix)
# ---------------------------------------------------------------------------
def _plans_yaml_candidates() -> list:
    here = os.path.dirname(os.path.abspath(__file__))  # shared/python/services
    cands = []
    env_path = _cfg("PLANS_YAML_PATH", None)
    if env_path:
        cands.append(env_path)
    cands.extend([
        os.path.join(os.getcwd(), "config", "plans.yaml"),            # repo root (local dev)
        os.path.join(here, "..", "config", "plans.yaml"),             # shared/python/config (if bundled)
        os.path.join(here, "..", "..", "..", "config", "plans.yaml"), # repo root from shared/python/services
    ])
    return cands


def _load_plans_yaml() -> Optional[Dict[str, Any]]:
    for path in _plans_yaml_candidates():
        try:
            if not path or not os.path.isfile(path):
                continue
            import yaml  # lazy
            with open(path, "r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh) or {}
            plans = data.get("plans")
            if isinstance(plans, dict) and plans:
                logger.info("stripe_service: loaded plan catalog from %s", path)
                return {str(k).lower(): dict(v or {}) for k, v in plans.items()}
        except Exception as e:
            logger.warning("stripe_service: failed to load plans.yaml at %s: %s", path, e)
    return None


def _fallback_catalog() -> Dict[str, Any]:
    """Derive a billing catalog from the entitlements matrix (yaml absent)."""
    from services import entitlements_service as ent
    catalog: Dict[str, Any] = {}
    for pid, meta in ent.PLAN_META.items():
        catalog[pid] = {
            "label": meta.get("label", pid),
            "entitlement_plan": pid,
            "price_monthly": meta.get("price_monthly", 0.0),
            "purchasable": pid != ent.FREE,
            "stripe_price_env": None if pid == ent.FREE else f"STRIPE_PRICE_{pid.upper()}",
        }
    return catalog


def _plans_catalog() -> Dict[str, Any]:
    global _PLANS_CACHE
    if _PLANS_CACHE is None:
        _PLANS_CACHE = _load_plans_yaml() or _fallback_catalog()
    return _PLANS_CACHE


def plan_catalog() -> Dict[str, Any]:
    """Public catalog view for the billing UI (safe when unconfigured)."""
    return {
        "plans": _plans_catalog(),
        "billing_enabled": billing_enabled(),
        "configured": is_configured(),
    }


def resolve_entitlement_plan(plan: str) -> Optional[str]:
    """Map a billing slug (or an entitlements id) to a canonical VALID_PLANS id.

    Accepts either the §6.10 slug (free/pro/career) or an entitlements id
    (free/premium/premium_plus). Returns None for anything unknown.
    """
    from services import entitlements_service as ent
    if not plan:
        return None
    p = str(plan).strip().lower()
    if p in ent.VALID_PLANS:
        return p
    entry = _plans_catalog().get(p) or {}
    ep = entry.get("entitlement_plan")
    return ep if ep in ent.VALID_PLANS else None


def _price_id(plan_slug: str) -> str:
    entry = _plans_catalog().get(str(plan_slug).strip().lower()) or {}
    direct = entry.get("stripe_price_id")
    if direct:
        return str(direct)
    env_name = entry.get("stripe_price_env")
    if env_name:
        return (_cfg(env_name, "") or "").strip()
    return ""


# ---------------------------------------------------------------------------
# URLs
# ---------------------------------------------------------------------------
def _frontend_url() -> str:
    from utils.config import AppUrlsConfig
    return AppUrlsConfig.FRONTEND_URL


def _success_url() -> str:
    return f"{_frontend_url()}/billing?status=success&session_id={{CHECKOUT_SESSION_ID}}"


def _cancel_url() -> str:
    return f"{_frontend_url()}/billing?status=cancelled"


def _return_url() -> str:
    return f"{_frontend_url()}/billing"


# ---------------------------------------------------------------------------
# Stripe customer <-> user mapping (stored on users.stripe_customer_id)
# ---------------------------------------------------------------------------
def _safe_email(user_email: str) -> str:
    return InputSanitizer.sanitize_email(user_email) or InputSanitizer.sanitize_string(user_email or "", 320)


def _ensure_customer(sdk, user_email: str) -> Optional[str]:
    email = _safe_email(user_email)
    if not email:
        return None
    db = _db()
    doc = db.users.find_one({"email": email}, {"stripe_customer_id": 1})
    cid = (doc or {}).get("stripe_customer_id")
    if cid:
        return cid
    customer = sdk.Customer.create(email=email, metadata={"user_email": email})
    db.users.update_one({"email": email}, {"$set": {"stripe_customer_id": customer.id}})
    return customer.id


def _email_for_customer(customer_id: str) -> Optional[str]:
    if not customer_id:
        return None
    safe = InputSanitizer.sanitize_string(str(customer_id), 120)
    doc = _db().users.find_one({"stripe_customer_id": safe}, {"email": 1})
    return (doc or {}).get("email")


# ---------------------------------------------------------------------------
# Checkout + Portal
# ---------------------------------------------------------------------------
def create_checkout_session(user_email: str, plan: str) -> Tuple[Dict[str, Any], int]:
    """Start a Stripe Checkout subscription session for `plan`.

    Returns (body, status). 503 = billing not configured, 400 = bad plan,
    502 = upstream Stripe error, 200 = {checkout_url}.
    """
    from services import entitlements_service as ent
    ent_plan = resolve_entitlement_plan(plan)
    if not ent_plan or ent_plan == ent.FREE:
        return {"error": "invalid_plan", "message": "Unknown or non-purchasable plan."}, 400

    if not is_configured():
        return _not_configured({"checkout_url": None, "requested_plan": plan})
    sdk = _stripe()
    if sdk is None:
        return _not_configured({"checkout_url": None, "requested_plan": plan})

    price_id = _price_id(plan)
    if not price_id:
        return _not_configured({
            "checkout_url": None,
            "requested_plan": plan,
            "message": "No Stripe price configured for this plan.",
        })

    email = _safe_email(user_email)
    if not email:
        return {"error": "invalid_email"}, 400

    try:
        customer_id = _ensure_customer(sdk, email)
        session = sdk.checkout.Session.create(
            mode="subscription",
            customer=customer_id,
            line_items=[{"price": price_id, "quantity": 1}],
            success_url=_success_url(),
            cancel_url=_cancel_url(),
            client_reference_id=email,
            metadata={"user_email": email, "plan_slug": str(plan).lower(), "entitlement_plan": ent_plan},
            subscription_data={"metadata": {"user_email": email, "entitlement_plan": ent_plan}},
            idempotency_key=f"checkout:{email}:{ent_plan}:{_day_key()}",
        )
        return {
            "configured": True,
            "checkout_url": session.url,
            "session_id": session.id,
            "plan": ent_plan,
        }, 200
    except Exception as e:
        logger.warning("stripe checkout failed for %s: %s", email, e)
        return {"error": "billing_error", "message": "Could not start checkout."}, 502


def create_portal_session(user_email: str) -> Tuple[Dict[str, Any], int]:
    """Open a Stripe billing-portal session so the user can manage/cancel.

    Returns (body, status). 503 unconfigured, 400 no customer yet, 502 error.
    """
    if not is_configured():
        return _not_configured({"portal_url": None})
    sdk = _stripe()
    if sdk is None:
        return _not_configured({"portal_url": None})

    email = _safe_email(user_email)
    if not email:
        return {"error": "invalid_email"}, 400

    doc = _db().users.find_one({"email": email}, {"stripe_customer_id": 1})
    cid = (doc or {}).get("stripe_customer_id")
    if not cid:
        return {"error": "no_customer", "message": "No billing account yet — start a checkout first."}, 400

    try:
        sess = sdk.billing_portal.Session.create(customer=cid, return_url=_return_url())
        return {"configured": True, "portal_url": sess.url}, 200
    except Exception as e:
        logger.warning("stripe portal failed for %s: %s", email, e)
        return {"error": "billing_error", "message": "Could not open billing portal."}, 502


# ---------------------------------------------------------------------------
# Webhook (signature-verified + idempotent). UNAUTHENTICATED route.
# ---------------------------------------------------------------------------
def _claim_event(event_id: str, etype: str) -> bool:
    """Atomically claim an event id in `stripe_events`. True if newly claimed."""
    try:
        res = _db().stripe_events.update_one(
            {"event_id": event_id},
            {"$setOnInsert": {
                "event_id": event_id,
                "type": etype,
                "status": "processing",
                "claimed_at": _now(),
            }},
            upsert=True,
        )
        return res.upserted_id is not None
    except Exception as e:
        # On a duplicate-key race the update path (no upsert) means already seen.
        logger.warning("stripe_events claim failed for %s: %s", event_id, e)
        return False


def _complete_event(event_id: str) -> None:
    try:
        _db().stripe_events.update_one(
            {"event_id": event_id},
            {"$set": {"status": "completed", "completed_at": _now()}},
        )
    except Exception as e:  # pragma: no cover
        logger.warning("stripe_events complete failed for %s: %s", event_id, e)


def _release_event(event_id: str) -> None:
    """Delete the claim so Stripe's retry can reprocess a failed handler."""
    try:
        _db().stripe_events.delete_one({"event_id": event_id, "status": "processing"})
    except Exception as e:  # pragma: no cover
        logger.warning("stripe_events release failed for %s: %s", event_id, e)


def _obj(event: Any) -> Dict[str, Any]:
    try:
        return event["data"]["object"] or {}
    except Exception:
        return {}


def _apply_plan(email: Optional[str], plan: Optional[str]) -> None:
    from services import entitlements_service as ent
    ent_plan = resolve_entitlement_plan(plan) if plan else None
    if not email or not ent_plan:
        logger.warning("webhook: cannot apply plan (email=%s plan=%s)", email, plan)
        return
    ent.set_plan(email, ent_plan)
    logger.info("webhook: set plan %s for %s", ent_plan, email)


def _dispatch_event(etype: str, event: Any) -> None:
    from services import entitlements_service as ent
    obj = _obj(event)

    if etype == "checkout.session.completed":
        meta = obj.get("metadata") or {}
        email = meta.get("user_email") or obj.get("client_reference_id") or _email_for_customer(obj.get("customer"))
        plan = meta.get("entitlement_plan") or meta.get("plan_slug")
        _apply_plan(email, plan)

    elif etype in ("customer.subscription.created", "customer.subscription.updated"):
        meta = obj.get("metadata") or {}
        email = meta.get("user_email") or _email_for_customer(obj.get("customer"))
        status = obj.get("status")
        if status in ("active", "trialing"):
            plan = meta.get("entitlement_plan")
            # Fall back to the price on the subscription's first item.
            if not plan:
                plan = _plan_from_subscription(obj)
            _apply_plan(email, plan)
        elif status in ("canceled", "unpaid", "incomplete_expired"):
            _apply_plan(email, ent.FREE)

    elif etype == "customer.subscription.deleted":
        meta = obj.get("metadata") or {}
        email = meta.get("user_email") or _email_for_customer(obj.get("customer"))
        _apply_plan(email, ent.FREE)

    elif etype == "invoice.payment_failed":
        email = _email_for_customer(obj.get("customer"))
        # Grace period: don't downgrade on a single failure — record + let the
        # subscription lifecycle events drive any actual downgrade.
        logger.warning("webhook: invoice.payment_failed for %s", email)
        try:
            _db().billing_incidents.insert_one({
                "type": "payment_failed",
                "email": email,
                "customer": obj.get("customer"),
                "invoice": obj.get("id"),
                "amount_due": obj.get("amount_due"),
                "at": _now(),
            })
        except Exception as e:  # pragma: no cover
            logger.warning("billing_incidents write failed: %s", e)

    else:
        logger.info("webhook: ignoring unhandled event type %s", etype)


def _plan_from_subscription(sub_obj: Dict[str, Any]) -> Optional[str]:
    """Best-effort: map the subscription's first price id back to a plan slug."""
    try:
        items = ((sub_obj.get("items") or {}).get("data")) or []
        if not items:
            return None
        price = (items[0].get("price") or {}).get("id")
        if not price:
            return None
        for slug, entry in _plans_catalog().items():
            if _price_id(slug) == price:
                return slug
    except Exception as e:  # pragma: no cover
        logger.warning("_plan_from_subscription failed: %s", e)
    return None


def handle_webhook(payload: bytes, sig_header: Optional[str]) -> Tuple[Dict[str, Any], int]:
    """Verify + process a Stripe webhook.

    Returns (body, status). 503 unconfigured, 400 bad signature, 200 ok/duplicate,
    500 handler error (so Stripe retries). Route MUST be unauthenticated and pass
    the RAW request body + the `Stripe-Signature` header.
    """
    if not billing_enabled() or not _webhook_secret():
        return _not_configured()
    sdk = _stripe()
    if sdk is None:
        return _not_configured()

    try:
        event = sdk.Webhook.construct_event(payload, sig_header, _webhook_secret())
    except Exception as e:
        logger.warning("stripe webhook signature verification failed: %s", e)
        return {"error": "invalid_signature"}, 400

    try:
        event_id = event["id"]
        etype = event["type"]
    except Exception:
        return {"error": "invalid_event"}, 400

    # Idempotency: atomically claim; a duplicate is a benign 200.
    if event_id and not _claim_event(event_id, etype):
        return {"ok": True, "duplicate": True, "type": etype}, 200

    try:
        _dispatch_event(etype, event)
    except Exception as e:
        logger.exception("stripe webhook handler error for %s: %s", etype, e)
        _release_event(event_id)  # allow Stripe to retry
        return {"error": "handler_error"}, 500

    _complete_event(event_id)
    return {"ok": True, "type": etype}, 200


# ---------------------------------------------------------------------------
# Entitlement overrides — permanent per-user admin escape hatch (§6.10)
# ---------------------------------------------------------------------------
def set_override(user_email: str, overrides: Optional[Dict[str, Any]],
                 actor: Optional[str] = None) -> Dict[str, Any]:
    """Set (or clear, if overrides is falsy) per-user entitlement overrides.

    `overrides` is a partial map that entitlements_service should layer ON TOP of
    the plan: e.g. {"plan": "premium_plus", "limits": {"tailor": 999},
    "features": ["referral_finder"]}. Stored in `entitlement_overrides`, keyed by
    user_email. This is permanent testing-phase infra, not a hack.
    """
    email = _safe_email(user_email)
    if not email:
        return {"error": "invalid_email"}
    db = _db()
    if not overrides:
        db.entitlement_overrides.delete_one({"user_email": email})
        return {"user_email": email, "overrides": None, "cleared": True}
    if not isinstance(overrides, dict):
        return {"error": "invalid_overrides", "message": "overrides must be an object."}
    db.entitlement_overrides.update_one(
        {"user_email": email},
        {"$set": {
            "user_email": email,
            "overrides": overrides,
            "updated_at": _now(),
            "updated_by": actor or "unknown",
        }},
        upsert=True,
    )
    return get_override(email)


def get_override(user_email: str) -> Dict[str, Any]:
    """Return the stored override doc for a user, or {} if none."""
    email = _safe_email(user_email)
    if not email:
        return {}
    try:
        doc = _db().entitlement_overrides.find_one({"user_email": email}, {"_id": 0})
    except Exception as e:
        logger.warning("get_override failed for %s: %s", email, e)
        return {}
    if not doc:
        return {}
    ua = doc.get("updated_at")
    if hasattr(ua, "isoformat"):
        doc["updated_at"] = ua.isoformat()
    return doc

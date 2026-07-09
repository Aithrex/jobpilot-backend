"""Billing / plan blueprint (/api/billing).

Freemium is enforced server-side by services/entitlements_service.py. This
blueprint exposes the current plan + live usage to the UI and a STUB upgrade
endpoint. Real payments (Stripe Checkout + a webhook that flips users.plan via
entitlements_service.set_plan) are roadmap — the upgrade endpoint returns a
"coming soon" payload today so the frontend can wire the flow now.
"""
import logging

from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity

from services import entitlements_service
from services import stripe_service

billing_bp = Blueprint("billing", __name__)
logger = logging.getLogger(__name__)


@billing_bp.route("/plan", methods=["GET"])
@jwt_required()
def get_plan():
    """Current plan, feature flags, limits, and live usage for the user."""
    return jsonify(entitlements_service.to_billing_view(get_jwt_identity())), 200


@billing_bp.route("/upgrade", methods=["POST"])
@jwt_required()
def upgrade():
    """Stub: returns checkout_url=null until Stripe is wired."""
    data = request.get_json(force=True, silent=True) or {}
    tier = (data.get("tier") or "").strip().lower()
    if tier and tier not in entitlements_service.VALID_PLANS:
        return jsonify({"error": "invalid_tier"}), 400
    return jsonify({
        "checkout_url": None,
        "requested_tier": tier or None,
        "message": "Billing is coming soon — your plan hasn't changed.",
    }), 200


# ------------------------------------------------------------------
# Stripe (§6.10) — dark behind BILLING_ENFORCEMENT; degrade to 503 with no keys.
# ------------------------------------------------------------------
@billing_bp.route("/catalog", methods=["GET"])
@jwt_required()
def catalog():
    return jsonify(stripe_service.plan_catalog()), 200


@billing_bp.route("/checkout", methods=["POST"])
@jwt_required()
def checkout():
    d = request.get_json(force=True, silent=True) or {}
    plan = (d.get("plan") or d.get("tier") or "").strip().lower()
    body, status = stripe_service.create_checkout_session(get_jwt_identity(), plan)
    return jsonify(body), status


@billing_bp.route("/portal", methods=["POST"])
@jwt_required()
def portal():
    body, status = stripe_service.create_portal_session(get_jwt_identity())
    return jsonify(body), status


@billing_bp.route("/webhook", methods=["POST"])  # UNAUTHENTICATED — verified by Stripe signature
def webhook():
    sig = request.headers.get("Stripe-Signature", "")
    body, status = stripe_service.handle_webhook(request.get_data(), sig)
    return jsonify(body), status

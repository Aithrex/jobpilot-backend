"""Feedback blueprint — user-submitted product feedback.

Used in two places:
  1. Floating "Send feedback" button (general comments, bug reports, ideas)
  2. The daily-quota-exceeded modal funnels users here to request a bump

Admin endpoints (list + respond) live in the auth service's admin blueprint.
"""
import logging
from datetime import datetime, timezone

from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity

from utils.db_connect import DBConnect
from utils.security import InputSanitizer, get_rate_limiter, get_client_ip

feedback_bp = Blueprint("feedback", __name__)
logger = logging.getLogger(__name__)

ALLOWED_TYPES = {"general", "quota_bump", "bug", "idea"}
MAX_MSG_LEN = 4000


@feedback_bp.route("/submit", methods=["POST"])
@jwt_required()
def submit_feedback():
    client_ip = get_client_ip(request)
    limiter = get_rate_limiter()
    if limiter.is_rate_limited(
        f"feedback:{client_ip}", max_requests=10, window_seconds=600
    ):
        return jsonify({"error": "Too many feedback submissions. Try again later."}), 429

    data = request.get_json(force=True) or {}
    message = (data.get("message") or "").strip()
    fb_type = (data.get("type") or "general").strip().lower()

    if not message:
        return jsonify({"error": "message is required"}), 400
    if len(message) > MAX_MSG_LEN:
        return jsonify({"error": f"message too long (max {MAX_MSG_LEN} chars)"}), 400
    if fb_type not in ALLOWED_TYPES:
        fb_type = "general"

    message = InputSanitizer.sanitize_string(message, max_length=MAX_MSG_LEN)
    if not message:
        return jsonify({"error": "Invalid message"}), 400

    user_email = get_jwt_identity()
    doc = {
        "email": user_email,
        "message": message,
        "type": fb_type,
        "status": "open",
        "created_at": datetime.now(timezone.utc),
        "admin_response": None,
        "responded_at": None,
    }
    try:
        db = DBConnect().get_db()
        result = db.feedback.insert_one(doc)
        return jsonify({"id": str(result.inserted_id), "status": "received"}), 201
    except Exception as e:
        logger.error(f"Feedback submit error: {e}")
        return jsonify({"error": "Could not save feedback"}), 500


@feedback_bp.route("/mine", methods=["GET"])
@jwt_required()
def my_feedback():
    """Return the current user's own feedback history (last 20).

    Lets users see admin responses to their quota-bump requests without
    needing email — they can check inside the app.
    """
    user_email = get_jwt_identity()
    try:
        db = DBConnect().get_db()
        docs = list(
            db.feedback.find({"email": user_email})
            .sort("created_at", -1)
            .limit(20)
        )
        def _iso(v):
            if not hasattr(v, "isoformat"):
                return None
            s = v.isoformat()
            return s if s.endswith("Z") else s + "Z"

        out = []
        for d in docs:
            out.append({
                "id": str(d["_id"]),
                "message": d.get("message", ""),
                "type": d.get("type", "general"),
                "status": d.get("status", "open"),
                "admin_response": d.get("admin_response"),
                "created_at": _iso(d.get("created_at")),
                "responded_at": _iso(d.get("responded_at")),
            })
        return jsonify({"feedback": out}), 200
    except Exception as e:
        logger.error(f"Feedback list error: {e}")
        return jsonify({"error": "Could not load feedback"}), 500

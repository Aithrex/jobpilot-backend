"""Voice Graph blueprint — writing-sample capture, consent, and (later) the
knowledge graph / authored resume.

Mounted at /api/resume/voice-graph so it rides the existing "ANY /api/resume/{proxy+}"
edge route — no new Terraform route needed (CLAUDE.md invariant #6).

Every route is gated by `voice_graph_enabled()`: when the flag is off (prod default)
the routes return 404, so the feature is invisible until the ship gate passes.

Auth contract: business errors are 400/403/404/429 — never 401 (401 is reserved for
missing/invalid JWT via @jwt_required()).
"""
import logging

from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity

from utils.security import get_rate_limiter, get_client_ip

from services.voicegraph.flags import voice_graph_enabled
from services.voicegraph import consent as consent_svc
from services.voicegraph import samples as sample_svc
from services.voicegraph import prompts as prompt_svc
from services.voicegraph.consent import CONSENT_TEXT_VERSION

voice_graph_bp = Blueprint("voice_graph", __name__)
logger = logging.getLogger(__name__)

_ALLOWED_SOURCES = {"typed", "voice"}
_WRITING_SAMPLES_METRIC = "writing_samples"


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------
def _gate():
    """Return a 404 response if the feature flag is off, else None."""
    if not voice_graph_enabled():
        return jsonify({"error": "not_found"}), 404
    return None


def _rate_limited(bucket: str, max_requests: int, window_seconds: int = 300) -> bool:
    try:
        ip = get_client_ip(request)
        return get_rate_limiter().is_rate_limited(
            f"voicegraph_{bucket}:{ip}", max_requests=max_requests, window_seconds=window_seconds
        )
    except Exception:
        return False


def _sample_cap(user_email: str):
    """(limit, is_capped) for finalized writing samples on the user's plan.
    Fails open (no cap) on any entitlement-lookup error — the feature is flagged
    off in prod anyway, and we never want a lookup blip to block a real user."""
    try:
        from services import entitlements_service as ent
        limit = ent.quota(user_email, _WRITING_SAMPLES_METRIC)
        return limit, (limit is not None)
    except Exception as e:
        logger.warning("voicegraph: sample cap lookup failed: %s", e)
        return None, False


# ------------------------------------------------------------------
# GET /api/resume/voice-graph/prompts — the shuffleable writing prompts
# ------------------------------------------------------------------
@voice_graph_bp.route("/prompts", methods=["GET"])
@jwt_required()
def get_prompts():
    gated = _gate()
    if gated:
        return gated
    return jsonify({"prompts": prompt_svc.list_prompts()}), 200


# ------------------------------------------------------------------
# GET /api/resume/voice-graph/status — flag + consent + sample summary
# ------------------------------------------------------------------
@voice_graph_bp.route("/status", methods=["GET"])
@jwt_required()
def status():
    gated = _gate()
    if gated:
        return gated
    user_email = get_jwt_identity()
    con = consent_svc.status(user_email)
    ready = sample_svc.count_ready(user_email)
    limit, _ = _sample_cap(user_email)
    return jsonify({
        "enabled": True,
        "consent": con,
        "consent_text_version": CONSENT_TEXT_VERSION,
        "samples": {
            "ready": ready,
            "cap": limit,                      # None == unlimited
            "ready_word_total": sample_svc.ready_word_total(user_email),
            "min_signal_words": sample_svc.MIN_SIGNAL_WORDS,
        },
    }), 200


# ------------------------------------------------------------------
# POST /api/resume/voice-graph/consent — grant Voice Graph consent
# DELETE .../consent — revoke
# ------------------------------------------------------------------
@voice_graph_bp.route("/consent", methods=["POST"])
@jwt_required()
def grant_consent():
    gated = _gate()
    if gated:
        return gated
    if _rate_limited("consent", max_requests=20):
        return jsonify({"error": "Rate limit exceeded. Try again shortly."}), 429
    user_email = get_jwt_identity()
    ip = get_client_ip(request)
    result = consent_svc.grant(user_email, ip=ip)
    return jsonify(result), 200


@voice_graph_bp.route("/consent", methods=["DELETE"])
@jwt_required()
def revoke_consent():
    gated = _gate()
    if gated:
        return gated
    user_email = get_jwt_identity()
    # NOTE: the full data-deletion cascade (samples + graph nodes whose only
    # evidence is a writing sample) lands in VG Step 8. This flips the ledger row.
    result = consent_svc.revoke(user_email)
    return jsonify(result), 200


# ------------------------------------------------------------------
# POST /api/resume/voice-graph/samples — create / autosave / finalize a sample
# ------------------------------------------------------------------
@voice_graph_bp.route("/samples", methods=["POST"])
@jwt_required()
def save_sample():
    gated = _gate()
    if gated:
        return gated
    # Autosave fires often; keep this bucket generous (~1/sec sustained).
    if _rate_limited("save", max_requests=300):
        return jsonify({"error": "Rate limit exceeded. Try again shortly."}), 429

    user_email = get_jwt_identity()
    if not consent_svc.is_granted(user_email):
        return jsonify({"error": "consent_required",
                        "message": "Grant Voice Graph consent before saving a writing sample."}), 403

    data = request.get_json(force=True, silent=True) or {}

    text = data.get("text", "")
    if not isinstance(text, str):
        return jsonify({"error": "text must be a string"}), 400

    sample_id = data.get("sample_id")
    if sample_id is not None and not isinstance(sample_id, str):
        return jsonify({"error": "sample_id must be a string"}), 400

    source = data.get("source", "typed")
    if source not in _ALLOWED_SOURCES:
        source = "typed"

    prompt_id = data.get("prompt_id")
    if prompt_id is not None and not (isinstance(prompt_id, str) and prompt_svc.is_valid_prompt(prompt_id)):
        prompt_id = None

    finalize = bool(data.get("finalize", False))

    # Was this sample already finalized? (Avoids re-enqueuing extraction on repeat
    # finalize calls.) Only relevant when updating an existing sample.
    prior_ready = False
    if finalize and isinstance(sample_id, str):
        existing = sample_svc.get_sample(user_email, sample_id)
        prior_ready = bool(existing and existing.get("status") == "ready")

    # Plan cap only applies when a NEW sample is being finalized into "ready".
    if finalize and not prior_ready:
        limit, capped = _sample_cap(user_email)
        if capped:
            current_ready = sample_svc.count_ready(user_email, exclude_sample_id=sample_id if isinstance(sample_id, str) else None)
            if current_ready >= limit:
                try:
                    from services import entitlements_service as ent
                    return ent.upgrade_required_response(user_email, _WRITING_SAMPLES_METRIC)
                except Exception:
                    return jsonify({"error": "upgrade_required",
                                    "message": f"Your plan allows {limit} writing sample(s)."}), 403

    try:
        result = sample_svc.save_sample(
            user_email,
            text=text,
            sample_id=sample_id,
            source=source,
            prompt_id=prompt_id,
            finalize=finalize,
            consent_version=CONSENT_TEXT_VERSION,
        )
    except KeyError:
        return jsonify({"error": "sample_not_found"}), 404
    except Exception as e:
        logger.error("voicegraph: save_sample failed: %s", e)
        return jsonify({"error": "Failed to save writing sample. Please try again."}), 500

    # A newly-finalized sample kicks off async graph extraction (§6.12.3). The FE
    # can poll /api/resume/job/<extraction_job_id> to reveal the graph when ready.
    if finalize and not prior_ready and result.get("status") == "ready":
        try:
            from services.resume_service import get_resume_service, ResumeService
            job_payload = {"sample_id": result["sample_id"], "user_email": user_email}
            job_id = get_resume_service().create_job("vg_extract", job_payload, user_email=user_email)
            ResumeService.invoke_async(job_id, "vg_extract", job_payload)
            result["extraction_job_id"] = job_id
        except Exception as e:
            logger.warning("voicegraph: failed to enqueue extraction: %s", e)

    return jsonify(result), 200


# ------------------------------------------------------------------
# GET /api/resume/voice-graph/samples — list the user's samples
# ------------------------------------------------------------------
@voice_graph_bp.route("/samples", methods=["GET"])
@jwt_required()
def list_samples():
    gated = _gate()
    if gated:
        return gated
    user_email = get_jwt_identity()
    return jsonify({"samples": sample_svc.list_samples(user_email)}), 200


# ------------------------------------------------------------------
# GET /api/resume/voice-graph/samples/<sample_id> — one sample
# DELETE .../samples/<sample_id> — delete one sample
# ------------------------------------------------------------------
@voice_graph_bp.route("/samples/<sample_id>", methods=["GET"])
@jwt_required()
def get_sample(sample_id):
    gated = _gate()
    if gated:
        return gated
    user_email = get_jwt_identity()
    doc = sample_svc.get_sample(user_email, sample_id)
    if not doc:
        return jsonify({"error": "sample_not_found"}), 404
    return jsonify(doc), 200


@voice_graph_bp.route("/samples/<sample_id>", methods=["DELETE"])
@jwt_required()
def delete_sample(sample_id):
    gated = _gate()
    if gated:
        return gated
    user_email = get_jwt_identity()
    ok = sample_svc.delete_sample(user_email, sample_id)
    if not ok:
        return jsonify({"error": "sample_not_found"}), 404
    return jsonify({"deleted": True, "sample_id": sample_id}), 200


# ------------------------------------------------------------------
# GET /api/resume/voice-graph/graph — the user's knowledge graph (reveal)
# ------------------------------------------------------------------
@voice_graph_bp.route("/graph", methods=["GET"])
@jwt_required()
def get_graph():
    gated = _gate()
    if gated:
        return gated
    user_email = get_jwt_identity()
    from services.voicegraph import store
    return jsonify(store.get_graph(user_email)), 200


# ------------------------------------------------------------------
# GET /api/resume/voice-graph/voice — the measured voice profile ("how you write")
# ------------------------------------------------------------------
@voice_graph_bp.route("/voice", methods=["GET"])
@jwt_required()
def get_voice_profile():
    gated = _gate()
    if gated:
        return gated
    user_email = get_jwt_identity()
    from services.voicegraph import style
    try:
        return jsonify(style.get_profile(user_email)), 200
    except Exception as e:
        logger.error("voicegraph: voice profile failed: %s", e)
        return jsonify({"error": "Failed to compute voice profile."}), 500


# ------------------------------------------------------------------
# POST /api/resume/voice-graph/resume/raw — zero-model resume (free, all tiers)
# GET  .../resume        — list authored resumes
# GET  .../resume/<id>   — one authored resume + provenance
# ------------------------------------------------------------------
@voice_graph_bp.route("/resume/raw", methods=["POST"])
@jwt_required()
def build_raw_resume_route():
    gated = _gate()
    if gated:
        return gated
    if _rate_limited("resume", max_requests=30):
        return jsonify({"error": "Rate limit exceeded. Try again shortly."}), 429
    user_email = get_jwt_identity()
    if not consent_svc.is_granted(user_email):
        return jsonify({"error": "consent_required"}), 403
    from services.voicegraph import raw_resume
    try:
        result = raw_resume.build_raw_resume(user_email)
        return jsonify(result), 200
    except ValueError:
        return jsonify({"error": "no_ready_samples",
                        "message": "Write and finalize at least one sample first, then we can build a resume from your own words."}), 400
    except Exception as e:
        logger.error("voicegraph: raw resume failed: %s", e)
        return jsonify({"error": "Failed to build resume."}), 500


@voice_graph_bp.route("/resume", methods=["GET"])
@jwt_required()
def list_authored_resumes():
    gated = _gate()
    if gated:
        return gated
    user_email = get_jwt_identity()
    from services.voicegraph import raw_resume
    return jsonify({"resumes": raw_resume.list_resumes(user_email)}), 200


@voice_graph_bp.route("/resume/<resume_id>", methods=["GET"])
@jwt_required()
def get_authored_resume(resume_id):
    gated = _gate()
    if gated:
        return gated
    user_email = get_jwt_identity()
    from services.voicegraph import raw_resume
    doc = raw_resume.get_resume(user_email, resume_id)
    if not doc:
        return jsonify({"error": "resume_not_found"}), 404
    return jsonify(doc), 200

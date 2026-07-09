"""Consent revocation cascade (§6.12.8).

Revoking the `voice_graph` scope deletes the user's writing samples and voice
profile, and drops every graph node/edge whose ONLY evidence is a writing sample
(nodes co-evidenced by the resume survive — handled for free by
store.purge_by_source, which only deletes rows left with no evidence). Already-
generated resumes are the user's asset: they are FROZEN and relabeled, not deleted.

This has a dedicated test and must pass before the feature ships.
"""
import logging
from typing import Any, Dict

from utils.db_connect import DBConnect

from services.voicegraph import store
from services.voicegraph import samples as sample_svc
from services.voicegraph import consent as consent_svc

logger = logging.getLogger(__name__)


def revoke_cascade(user_email: str) -> Dict[str, Any]:
    # 1. Graph: remove writing-sample evidence, then any node/edge with no evidence
    #    left. Resume-co-evidenced nodes (after entity resolution) survive.
    graph_counts = store.purge_by_source(user_email, "writing_sample")

    # 2. Delete the writing samples themselves.
    samples_deleted = sample_svc.delete_all_samples(user_email)

    # 3. Delete the voice profile.
    try:
        style_deleted = DBConnect().get_db().style_profiles.delete_many(
            {"user_email": user_email}
        ).deleted_count
    except Exception as e:
        logger.warning("voicegraph revoke: style delete failed: %s", e)
        style_deleted = 0

    # 4. Delete entity-resolution merge candidates — they hold writing-derived
    #    labels and resume values in plaintext, so they must go too.
    try:
        candidates_deleted = DBConnect().get_db().merge_candidates.delete_many(
            {"user_email": user_email}
        ).deleted_count
    except Exception as e:
        logger.warning("voicegraph revoke: merge_candidates delete failed: %s", e)
        candidates_deleted = 0

    # 5. Freeze (do NOT delete) already-generated resumes — the user's asset.
    from services.voicegraph import raw_resume
    frozen = raw_resume.freeze_all_for_user(user_email)

    # 6. Flip the consent ledger row.
    consent_svc.revoke(user_email)

    summary = {
        "graph_nodes_removed": graph_counts.get("nodes_removed", 0),
        "graph_edges_removed": graph_counts.get("edges_removed", 0),
        "samples_deleted": samples_deleted,
        "style_profiles_deleted": style_deleted,
        "merge_candidates_deleted": candidates_deleted,
        "resumes_frozen": frozen,
    }
    logger.info("voicegraph revoke cascade for user: %s", summary)
    return summary

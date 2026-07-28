"""The review gate for MODEL-WRITTEN free text.

`answer_generator` stamps every generated record `review_required: True`. That
used to cap `auto` -> `hold` unconditionally, so any posting asking "why us?"
waited for a human — which defeats applying overnight. The product owner chose
hands-off submission, so the default is now OFF (auto-submit), kept as a
per-user setting so reverting is one write rather than a deploy.

What must stay true, and is pinned here:

  * default is auto-submit (the owner's decision);
  * the gate still works when a user turns it ON;
  * it governs generated FREE TEXT only — it can never relax the hard rules. An
    unconfirmed legal attestation still forces `ask` and an oracle wall still
    forces `hold`, whatever this flag says. That boundary is the whole reason
    the flag is narrow, and it is what stops "auto-submit my essays" from
    quietly becoming "auto-submit my visa status".
"""
import services.autonomy_service as autonomy

from _vg_support import current_db

USER = "prose@aspirely.me"


def _settings(**kw):
    current_db().auto_submit_settings.delete_many({"user_email": USER})
    if kw:
        current_db().auto_submit_settings.insert_one({"user_email": USER, **kw})


class TestDefault:
    def test_default_is_auto_submit(self):
        """The owner's decision. Unset must NOT hold."""
        _settings()
        assert autonomy.review_generated_prose(USER) is False
        assert autonomy.DEFAULT_REVIEW_GENERATED_PROSE is False

    def test_unknown_user_defaults_rather_than_raising(self):
        assert autonomy.review_generated_prose("nobody@nowhere.test") is False

    def test_empty_email_defaults(self):
        assert autonomy.review_generated_prose("") is False


class TestOptIn:
    def test_a_user_can_turn_review_back_on(self):
        _settings(review_generated_prose=True)
        assert autonomy.review_generated_prose(USER) is True

    def test_explicit_false_is_honoured(self):
        _settings(review_generated_prose=False)
        assert autonomy.review_generated_prose(USER) is False

    def test_truthy_values_coerce(self):
        for raw in (1, "yes", True):
            _settings(review_generated_prose=raw)
            assert autonomy.review_generated_prose(USER) is True


class TestScopeBoundary:
    """The flag must never widen beyond generated prose."""

    def test_it_cannot_relax_an_unconfirmed_legal_attestation(self):
        """Even with review off and a perfect score, an undeclared legal
        attestation still forces `ask`. Auto-submitting an ESSAY must never
        become auto-submitting a work-authorization claim."""
        _settings(review_generated_prose=False, autonomy_threshold=0.1)
        assert autonomy.decide(
            USER, score=1.0,
            has_unconfirmed_legal_attestation=True,
            blocked_on_oracle=False,
        ) == autonomy.ASK

    def test_it_cannot_relax_an_oracle_wall(self):
        _settings(review_generated_prose=False, autonomy_threshold=0.1)
        assert autonomy.decide(
            USER, score=1.0,
            has_unconfirmed_legal_attestation=False,
            blocked_on_oracle=True,
        ) == autonomy.HOLD

    def test_a_clean_high_score_still_autos(self):
        _settings(review_generated_prose=False, autonomy_threshold=0.6)
        assert autonomy.decide(
            USER, score=0.776,
            has_unconfirmed_legal_attestation=False,
            blocked_on_oracle=False,
        ) == autonomy.AUTO

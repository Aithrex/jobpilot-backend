"""The answer library must not submit an answer that does not fit the question.

Two live wrong-answer paths, plus the routing that lets a resume-grounded
generator answer an employer-specific prose question instead of stranding it
in the one-tap queue.
"""
import pytest

from services import answer_library_service as als
from services import application_question_bank as qb


def _q(label, *, field_id=None, value_type="", options=None, required=True):
    resolved = qb.normalize_field(label, options)
    fid = resolved.get("field_id")
    if field_id is None:
        field_id = fid if fid and fid != "unknown" else f"custom_{abs(hash(label)) % 9999}"
    return {"field_id": field_id, "label": label, "question": label,
            "required": required, "value_type": value_type, "options": options}


class TestAliasCoverage:
    """Questions a real form asked that the bank answered "unknown" for,
    stranding a resolvable question in the one-tap queue."""

    @pytest.mark.parametrize("label,field_id", [
        ("Current or Most Recent Employer", "current_company"),
        ("Most Recent Employer", "current_company"),
        ("Current or Most Recent Title", "current_title"),
        ("What certificates and/or licenses do you have?", "certifications"),
        ("Why are you interested in this role?", "why_company"),
        ("Why are you interested in this position?", "why_company"),
        ("What drew you to this opportunity?", "why_company"),
        ("Describe your experience with AWS.", "why_you"),
        ("Tell us about your experience in distributed systems", "why_you"),
    ])
    def test_live_phrasings_now_classify(self, label, field_id):
        assert qb.normalize_field(label)["field_id"] == field_id

    @pytest.mark.parametrize("label,field_id", [
        ("Country", "country"),
        ("LinkedIn Profile", "linkedin_url"),
        ("What is your desired salary?", "desired_salary"),
        ("Are you legally authorized to work in the United States?",
         "work_authorized_us"),
        ("Will you now or in the future require sponsorship?",
         "requires_sponsorship"),
        ("How did you hear about this job?", "how_did_you_hear"),
        ("Highest level of education completed", "highest_education"),
    ])
    def test_existing_classifications_are_untouched(self, label, field_id):
        assert qb.normalize_field(label)["field_id"] == field_id


class TestIsOpenProse:
    """Only a genuine prose box may be routed to generation. An enumerated
    control must never be: a generated string fails `no_option_match`, re-parks
    the row AND banks a poisoned answer for every future application."""

    def test_textarea_is_prose(self):
        assert als._is_open_prose({"value_type": "textarea", "label": "x"}) is True

    def test_any_options_list_disqualifies(self):
        assert als._is_open_prose(
            {"value_type": "textarea", "options": ["A", "B"], "label": "x"}) is False

    @pytest.mark.parametrize("vt", ["select", "radio", "checkbox", "date",
                                    "number", "file", "url"])
    def test_declared_non_prose_shapes_are_rejected(self, vt):
        assert als._is_open_prose({"value_type": vt, "label": "x" * 200}) is False

    def test_unknown_shape_needs_a_long_question(self):
        """ATS recon often reports a bare `text` type for a long question, so
        length is the only remaining signal. The threshold is
        `_PROSE_MIN_LABEL`; both sides of it are pinned here."""
        assert als._is_open_prose({"value_type": "", "label": "Badge number"}) is False

        short = "x" * (als._PROSE_MIN_LABEL - 1)
        long = "x" * als._PROSE_MIN_LABEL
        assert als._is_open_prose({"value_type": "", "label": short}) is False
        assert als._is_open_prose({"value_type": "", "label": long}) is True

        assert als._is_open_prose(
            {"value_type": "",
             "label": "What would you build first if you joined us next "
                      "Monday morning?"}) is True


class TestScalarIntoProseBox:
    """A 120-character essay prompt was being answered with a bare URL, because
    the classifier matched the word "portfolio" inside it."""

    LABEL = ("Our team values written communication above all. Please share a "
             "link to something you wrote and say why you chose it.")

    def test_scalar_field_into_a_prose_question_is_refused(self):
        assert als._scalar_answer_to_prose_question(
            {"value_type": "textarea", "label": self.LABEL}, "url") is True

    def test_scalar_field_into_a_scalar_question_is_fine(self):
        assert als._scalar_answer_to_prose_question(
            {"value_type": "url", "label": "LinkedIn Profile"}, "url") is False

    def test_prose_field_into_a_prose_question_is_fine(self):
        assert als._scalar_answer_to_prose_question(
            {"value_type": "textarea", "label": self.LABEL}, "essay") is False


class TestValueMustFitTheFormsOptions:
    """"Which shift are you available for?" matched the token "available" and
    resolved to `available_start_date`, so a Day/Night dropdown would have been
    answered "2026-07-06"."""

    def test_a_value_no_option_matches_is_refused(self):
        assert als._value_fits_options(
            "how_did_you_hear", "Company website",
            ["Job board", "Career fair"]) is False

    def test_a_value_an_option_matches_is_accepted(self):
        assert als._value_fits_options(
            "how_did_you_hear", "Company website",
            ["LinkedIn", "Company website", "Referral"]) is True

    def test_no_option_list_means_nothing_to_check(self):
        assert als._value_fits_options("country", "United States", None) is True

    def test_option_matching_tolerates_spelling(self):
        assert als._value_fits_options(
            "country", "United States of America",
            ["United States", "India", "Canada"]) is True


class TestOpenEndedFlagIsNarrow:
    """`open_ended` is what lets a non-catalog field reach the generator, so
    nothing but the library may set it."""

    def test_generator_accepts_a_catalog_id_without_the_flag(self):
        from services import answer_generator

        assert "why_company" in answer_generator._GENERATABLE_FIELD_IDS

    def test_generator_skips_an_unknown_id_without_the_flag(self, monkeypatch):
        from services import answer_generator

        calls = []
        monkeypatch.setattr(answer_generator, "AnswerGenerator",
                            lambda *a, **k: calls.append(1))
        out = answer_generator.generate_free_text_answers(
            "someone@example.com",
            [{"field_id": "custom_1", "question": "Describe a system you built."}],
            base_resume_doc={"structured": {"summary": "Engineer"}})
        assert out == {}

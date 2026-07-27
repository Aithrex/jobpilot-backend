"""Offline tests for the AUTO-APPLY REPAIR SPEC §0-§2 resolution fixes.

Runs fully offline (mongomock; no network, no LLM). What these lock in:

  * §2 — `_load_canonical` resolves EVERY catalog field, in the order
    user_preferences -> visa_profiles -> resume_assets.structured. The live bug
    this fixes: `apply_onboarding_service.READY_REQUIRED_FIELDS` forced the user
    to declare address_line1 / city / state / postal_code / country /
    phone_device_type / current_title / current_company / work_start_date /
    role_description, onboarding wrote them as flat `user_preferences[field_id]`
    keys, and the resolver read back NONE of them.
  * §2 — declared values arrive with MIXED types ('No' vs False vs True) and are
    coerced to the shape the field / option-matcher expects.
  * §2 — `contact.location` is decomposed into city/state/postal_code/country,
    and left UNRESOLVED when it cannot be parsed (never guessed).
  * §1 — the résumé may ONLY escalate a worked-here question to "Yes"; it may
    NEVER be used to assert "No".
  * §0.3 — protected EEO auto-declines; work-auth / sponsorship / citizenship /
    criminal history are NEVER auto-defaulted.
  * §3 — `remember()` / `remember_many()` are callable and stop the same
    question being asked twice.
"""
import pytest

from _vg_support import current_db

import services.answer_library_service as lib
import services.application_question_bank as qb
import services.apply_onboarding_service as onb

USER = "alice@aspirely.me"
OTHER = "bob@aspirely.me"


_COLLECTIONS = ("application_answer_library", "user_resumes", "resume_assets",
                "users", "user_preferences", "visa_profiles",
                "application_answers")


@pytest.fixture(autouse=True)
def db():
    """Clean BEFORE and AFTER — several later modules (e.g. test_autonomy_modes)
    seed their own resume_assets / user_preferences without clearing them first,
    so leaking a row from here would break them in file order."""
    d = current_db()
    for c in _COLLECTIONS:
        d[c].delete_many({})
    yield d
    for c in _COLLECTIONS:
        d[c].delete_many({})


def _seed_resume(d, structured, user=USER):
    d.resume_assets.insert_one({
        "resume_id": f"asset-{user}", "owner_type": "user", "user_email": user,
        "structured": structured, "parse_status": "parsed",
        "parsed_at": "2026-01-01T00:00:00+00:00",
    })


def _nq(field_id, *, required=True, options=None, invert=False, label=None):
    """A normalized question the way `apply_forms.normalize_recon_questions`
    builds it — field_id-keyed, with the bank's own sensitivity flag."""
    field = qb.get_field(field_id)
    return {
        "field_id": field_id,
        "label": label or (field["label"] if field else field_id),
        "type": "text",
        "required": required,
        "is_legal_attestation": bool(
            field and field["sensitivity"] in (qb.SENS_PROTECTED, qb.SENS_LEGAL)),
        "invert": invert,
        "options": options if options is not None else (
            list(field["options"]) if field and field["options"] else None),
    }


def _values(user=USER, company=None):
    return lib._load_canonical(user, company=company)["values"]


# ---------------------------------------------------------------------------
# §2 — the live bug: declared onboarding answers are read back
# ---------------------------------------------------------------------------
# Exactly the fields READY_REQUIRED_FIELDS forces the user to declare but the
# resolver used to report unresolved, plus the rest of the previously-missing 27.
_DECLARED = {
    "middle_name": "Q",
    "preferred_name": "Ali",
    "pronouns": "she/her",
    "phone_device_type": "Mobile",
    "address_line1": "1216 E Vista Del Cerro Dr",
    "address_line2": "Apt 4",
    "city": "Tempe",
    "state": "AZ",
    "postal_code": "85281",
    "country": "United States",
    "website_url": "https://alice.dev",
    "age_18_or_older": True,
    "government_employee": False,
    "dod_employee": False,
    "family_member_auditor": False,
    "ey_employee": False,
    "noncompete_agreement": False,
    "own_ip_rights": False,
    "secondary_employment_intent": "Neither",
    "previous_employee": "No",
    "current_company": "Globex",
    "current_title": "Staff Engineer",
    "work_start_date": "01/2022",
    "work_end_date": "12/2024",
    "currently_work_here": False,
    "role_description": "Owned the billing platform.",
    "has_security_clearance": False,
    "relatives_employed": False,
}


class TestDeclaredPrefsAreReadBack:
    def test_every_declared_field_resolves(self, db):
        db.user_preferences.insert_one({"user_email": USER, **_DECLARED})
        out = lib.resolve(USER, [_nq(fid) for fid in _DECLARED])
        missing = [fid for fid in _DECLARED if fid not in out["resolved"]]
        assert missing == [], f"declared but unresolved: {missing}"
        assert out["unresolved"] == []
        assert out["coverage"] == 1.0

    def test_ready_required_fields_all_have_a_resolution_path(self, db):
        """The READY gate must never demand something the resolver cannot read
        back — that combination is the live bug."""
        db.user_preferences.insert_one({
            "user_email": USER,
            "full_name": "Alice Q Smith", "email": "alice@aspirely.me",
            "phone": "+1-555-0100", "phone_device_type": "Mobile",
            "address_line1": "1 Main St", "city": "Tempe", "state": "AZ",
            "postal_code": "85281", "country": "United States",
            "work_authorized_us": True, "requires_sponsorship": False,
            "current_title": "Staff Engineer", "current_company": "Globex",
            "work_start_date": "01/2022",
            "role_description": "Owned the billing platform.",
            "over_18": True,
        })
        out = lib.resolve(USER, [_nq(f) for f in onb.READY_REQUIRED_FIELDS])
        assert out["unresolved"] == []
        assert out["has_unconfirmed_legal_attestation"] is False
        assert out["coverage"] == 1.0

    def test_every_catalog_field_has_a_key_in_values(self, db):
        """Spec §2: EVERY field in the catalog gets a resolution path (a key in
        `values`, a gated load in `legal`, or free-text generation) — none may be
        silently absent from the resolver's view."""
        values = _values()
        legal_ids = qb.gated_field_ids()
        for f in qb.FIELDS:
            fid = f["field_id"]
            if f["category"] == qb.CAT_FREE_TEXT or fid in legal_ids:
                continue
            assert fid in values, f"{fid} has no resolution path"

    def test_declared_pref_beats_resume(self, db):
        """Spec §2 order: user_preferences (1) outranks resume_assets (3)."""
        _seed_resume(db, {"contact": {"name": "Alice Smith",
                                      "email": "resume@old.test"}})
        db.user_preferences.insert_one(
            {"user_email": USER, "email": "declared@new.test"})
        out = lib.resolve(USER, [_nq("email")])
        assert out["resolved"]["email"]["value"] == "declared@new.test"

    def test_undeclared_field_still_unresolved(self, db):
        """The sweep must not invent values for fields the user never declared."""
        db.user_preferences.insert_one({"user_email": USER, "city": "Tempe"})
        out = lib.resolve(USER, [_nq("city"), _nq("has_drivers_license")])
        assert out["resolved"]["city"]["value"] == "Tempe"
        assert "has_drivers_license" in out["unresolved"]

    def test_declared_full_name_redrives_first_and_last(self, db):
        _seed_resume(db, {"contact": {"name": "Alicia Smythe"}})
        db.user_preferences.insert_one(
            {"user_email": USER, "full_name": "Alice Q Smith"})
        v = _values()
        assert v["full_name"] == "Alice Q Smith"
        assert v["first_name"] == "Alice"
        assert v["last_name"] == "Q Smith"


# ---------------------------------------------------------------------------
# §2 — mixed declared types coerce to what the option matcher expects
# ---------------------------------------------------------------------------
class TestDeclaredTypeCoercion:
    @pytest.mark.parametrize("declared", ["No", "no", "NO", False, 0])
    def test_falsy_spellings_on_a_yes_no_select(self, db, declared):
        db.user_preferences.insert_one(
            {"user_email": USER, "previous_employee": declared})
        hit = lib.resolve(USER, [_nq("previous_employee")])["resolved"]["previous_employee"]
        assert hit["value"] == "No"
        # and it still option-matches against a real form's wording.
        assert qb.match_option("previous_employee", hit["value"],
                               ["Yes", "No"])["option"] == "No"

    @pytest.mark.parametrize("declared", ["Yes", True, 1])
    def test_truthy_spellings_on_a_yes_no_select(self, db, declared):
        db.user_preferences.insert_one(
            {"user_email": USER, "previous_employee": declared})
        hit = lib.resolve(USER, [_nq("previous_employee")])["resolved"]["previous_employee"]
        assert hit["value"] == "Yes"

    @pytest.mark.parametrize("declared,expected", [
        ("No", False), (False, False), ("Yes", True), (True, True)])
    def test_boolean_field_becomes_a_real_bool(self, db, declared, expected):
        db.user_preferences.insert_one(
            {"user_email": USER, "government_employee": declared})
        hit = lib.resolve(USER, [_nq("government_employee")])["resolved"]["government_employee"]
        assert hit["value"] is expected

    def test_coerced_boolean_still_inverts_correctly(self, db):
        """A declared 'No' string must flip to True on an inverse-polarity form."""
        db.user_preferences.insert_one(
            {"user_email": USER, "willing_to_travel": "No"})
        out = lib.resolve(USER, [_nq("willing_to_travel", invert=True)])
        assert out["resolved"]["willing_to_travel"]["value"] is True

    def test_non_boolean_value_passes_through_untouched(self, db):
        db.user_preferences.insert_one(
            {"user_email": USER, "notice_period": "2 weeks"})
        hit = lib.resolve(USER, [_nq("notice_period")])["resolved"]["notice_period"]
        assert hit["value"] == "2 weeks"


# ---------------------------------------------------------------------------
# §2 — contact.location decomposition
# ---------------------------------------------------------------------------
class TestLocationDecomposition:
    @pytest.mark.parametrize("location,expected", [
        ("Tempe, AZ 85281", {"city": "Tempe", "state": "AZ", "postal_code": "85281"}),
        ("Tempe, AZ", {"city": "Tempe", "state": "AZ"}),
        ("San Francisco, California", {"city": "San Francisco", "state": "California"}),
        ("New York, NY 10001-1234",
         {"city": "New York", "state": "NY", "postal_code": "10001-1234"}),
        ("Austin, TX, United States",
         {"city": "Austin", "state": "TX", "country": "United States"}),
        ("Bangalore, Karnataka, India",
         {"city": "Bangalore", "state": "Karnataka", "country": "India"}),
        ("Hyderabad, India", {"city": "Hyderabad", "country": "India"}),
    ])
    def test_parsable_lines(self, location, expected):
        assert lib._decompose_location(location) == expected

    @pytest.mark.parametrize("location", ["", None, "Remote", "Austin",
                                          "Open to anywhere", "EMEA"])
    def test_unparsable_lines_yield_nothing(self, location):
        """Spec: leave unresolved rather than guess. 'Remote' is not a city."""
        assert lib._decompose_location(location) == {}

    def test_resolver_fills_city_state_zip_from_resume_location(self, db):
        _seed_resume(db, {"contact": {"name": "Alice Smith",
                                      "location": "Tempe, AZ 85281"}})
        out = lib.resolve(USER, [_nq("city"), _nq("state"), _nq("postal_code")])
        assert out["resolved"]["city"]["value"] == "Tempe"
        assert out["resolved"]["state"]["value"] == "AZ"
        assert out["resolved"]["postal_code"]["value"] == "85281"
        assert out["coverage"] == 1.0

    def test_unparsable_location_leaves_city_unresolved(self, db):
        _seed_resume(db, {"contact": {"name": "Alice Smith", "location": "Remote"}})
        out = lib.resolve(USER, [_nq("city")])
        assert out["resolved"] == {}
        assert "city" in out["unresolved"]

    def test_declared_city_beats_a_resume_location(self, db):
        _seed_resume(db, {"contact": {"location": "Tempe, AZ 85281"}})
        db.user_preferences.insert_one({"user_email": USER, "city": "Phoenix"})
        assert _values()["city"] == "Phoenix"


# ---------------------------------------------------------------------------
# §2 — résumé-derived employment / education facts
# ---------------------------------------------------------------------------
class TestResumeDerivedFacts:
    def test_experience_block_fills_workday_fields(self, db):
        _seed_resume(db, {"experience": [{
            "title": "Staff Engineer", "company": "Globex",
            "dates": "Jan 2022 - Present",
            "bullets": ["Owned billing.", "Cut latency 40%."]}]})
        v = _values()
        assert v["current_title"] == "Staff Engineer"
        assert v["current_company"] == "Globex"
        assert v["work_start_date"] == "01/2022"
        assert v["work_end_date"] is None          # still there — never invented
        assert v["currently_work_here"] is True
        assert "Owned billing." in v["role_description"]

    def test_ended_role_gets_an_end_date_and_not_current(self, db):
        _seed_resume(db, {"experience": [{
            "title": "SDE", "company": "Initech", "dates": "2019 – 2021"}]})
        v = _values()
        assert v["work_start_date"] == "2019"
        assert v["work_end_date"] == "2021"
        assert v["currently_work_here"] is False

    def test_unreadable_dates_stay_unresolved(self, db):
        _seed_resume(db, {"experience": [{
            "title": "SDE", "company": "Initech", "dates": "a while ago"}]})
        v = _values()
        assert v["work_start_date"] is None
        assert v["work_end_date"] is None
        assert v["currently_work_here"] is None

    def test_education_block(self, db):
        _seed_resume(db, {"education": [{
            "degree": "Master of Science in Computer Science",
            "institution": "Arizona State University",
            "dates": "2021 - 2023", "gpa": "3.9"}]})
        v = _values()
        assert v["school"] == "Arizona State University"
        assert v["degree"] == "Master of Science in Computer Science"
        assert v["field_of_study"] == "Computer Science"
        assert v["graduation_year"] == "2023"
        assert v["gpa"] == "3.9"
        assert v["highest_education"] == "Master's"

    def test_unrecognised_degree_leaves_highest_education_unresolved(self, db):
        _seed_resume(db, {"education": [{"degree": "Nanodegree in Widgets",
                                         "institution": "Somewhere"}]})
        assert _values()["highest_education"] is None

    def test_years_experience_is_never_computed(self, db):
        """A metric we never measured is asked, not derived."""
        _seed_resume(db, {"experience": [
            {"title": "SDE", "company": "A", "dates": "2015 - 2020"},
            {"title": "SDE2", "company": "B", "dates": "2020 - Present"}]})
        assert _values()["years_experience"] is None


# ---------------------------------------------------------------------------
# §1 — worked here / relatives / subsidiary
# ---------------------------------------------------------------------------
class TestWorkedHereFamily:
    def test_declared_no_is_read_back_when_no_resume_match(self, db):
        _seed_resume(db, {"experience": [{"company": "Globex", "title": "SDE"}]})
        db.user_preferences.insert_one({
            "user_email": USER, "previous_employee": "No", "ey_employee": False,
            "family_member_auditor": False, "government_employee": False,
            "dod_employee": False})
        out = lib.resolve(USER, [
            _nq("previous_employee"), _nq("ey_employee"),
            _nq("family_member_auditor"), _nq("government_employee"),
            _nq("dod_employee")], company="Acme Inc")
        assert out["unresolved"] == []
        assert out["resolved"]["previous_employee"]["value"] == "No"
        assert out["resolved"]["previous_employee"]["source"] == lib.SOURCE_DECLARED
        for fid in ("ey_employee", "family_member_auditor",
                    "government_employee", "dod_employee"):
            assert out["resolved"][fid]["value"] is False

    def test_resume_escalates_to_yes_and_flags_review(self, db):
        _seed_resume(db, {"experience": [
            {"company": "Acme Corporation", "title": "SDE"}]})
        db.user_preferences.insert_one(
            {"user_email": USER, "previous_employee": "No"})
        out = lib.resolve(USER, [_nq("previous_employee")], company="Acme, Inc.")
        hit = out["resolved"]["previous_employee"]
        assert hit["value"] == "Yes"
        assert hit["source"] == lib.SOURCE_RESUME_MATCH
        assert hit["review_required"] is True
        assert hit["escalated_by_resume"] is True

    def test_resume_never_asserts_no(self, db):
        """The résumé is a curated document, not an employment record. With no
        declared value and no company match, the question stays UNRESOLVED — the
        résumé must never be read as 'they have not worked here'."""
        _seed_resume(db, {"experience": [{"company": "Globex", "title": "SDE"}]})
        out = lib.resolve(USER, [_nq("previous_employee")], company="Acme Inc")
        assert out["resolved"] == {}
        assert "previous_employee" in out["unresolved"]

    def test_no_company_match_leaves_declared_no_intact(self, db):
        _seed_resume(db, {"experience": [{"company": "Globex", "title": "SDE"}]})
        db.user_preferences.insert_one(
            {"user_email": USER, "previous_employee": "No"})
        out = lib.resolve(USER, [_nq("previous_employee")], company="Acme Inc")
        assert out["resolved"]["previous_employee"]["value"] == "No"
        assert out["resolved"]["previous_employee"]["source"] == lib.SOURCE_DECLARED

    def test_escalation_needs_the_company_argument(self, db):
        """Omitting `company` is always safe — the escalation simply never fires."""
        _seed_resume(db, {"experience": [{"company": "Acme Corp", "title": "SDE"}]})
        db.user_preferences.insert_one(
            {"user_email": USER, "previous_employee": "No"})
        out = lib.resolve(USER, [_nq("previous_employee")])
        assert out["resolved"]["previous_employee"]["value"] == "No"

    def test_ey_employee_escalates_from_the_resume(self, db):
        _seed_resume(db, {"experience": [
            {"company": "Ernst & Young LLP", "title": "Consultant"}]})
        db.user_preferences.insert_one({"user_email": USER, "ey_employee": False})
        out = lib.resolve(USER, [_nq("ey_employee")], company="Acme Inc")
        assert out["resolved"]["ey_employee"]["value"] is True
        assert out["resolved"]["ey_employee"]["source"] == lib.SOURCE_RESUME_MATCH

    def test_a_declared_yes_is_never_downgraded(self, db):
        _seed_resume(db, {"experience": [{"company": "Globex", "title": "SDE"}]})
        db.user_preferences.insert_one(
            {"user_email": USER, "previous_employee": "Yes"})
        out = lib.resolve(USER, [_nq("previous_employee")], company="Acme Inc")
        assert out["resolved"]["previous_employee"]["value"] == "Yes"
        assert out["resolved"]["previous_employee"]["source"] == lib.SOURCE_DECLARED

    @pytest.mark.parametrize("a,b,expected", [
        ("Acme Inc", "Acme Corporation", True),
        ("Acme, Inc.", "ACME", True),
        ("Ernst & Young", "Ernst and Young LLP", True),
        ("Acme Inc", "Globex", False),
        ("EY", "Everyday Health", False),      # short name must not smear
        ("Meta", "Metabolic Labs", False),     # word-boundary, not substring
    ])
    def test_company_fuzzy_matching_is_conservative(self, a, b, expected):
        assert lib._company_matches(a, b) is expected

    def test_relatives_employed_is_a_real_catalog_field(self):
        f = qb.get_field("relatives_employed")
        assert f is not None
        assert f["category"] == qb.CAT_SCREENING
        assert f["sensitivity"] == qb.SENS_SAFE
        assert f["options"] == ["Yes", "No"]

    @pytest.mark.parametrize("label", [
        "Do you have any relatives employed by this company?",
        "Do you have friends or relatives working here?",
        "Are any of your family members currently employed by us?",
        "Do you have a relative employed at any of our subsidiaries?",
        "Are you related to any current employee?",
    ])
    def test_relatives_phrasings_map_to_relatives_employed(self, label):
        assert qb.normalize_field(label, allow_llm=False)["field_id"] == \
            "relatives_employed"

    @pytest.mark.parametrize("label", [
        "Have you previously been employed by this company or any of its subsidiaries?",
        "Have you ever worked for us or an affiliate?",
        "Have you previously worked at this organization?",
        "Are you eligible for rehire?",
    ])
    def test_subsidiary_phrasings_map_to_previous_employee(self, label):
        assert qb.normalize_field(label, allow_llm=False)["field_id"] == \
            "previous_employee"

    def test_ey_auditor_wording_still_wins_over_generic_relatives(self):
        label = ("Do you have an immediate family member who is a partner "
                 "at Ernst & Young?")
        assert qb.normalize_field(label, allow_llm=False)["field_id"] == \
            "family_member_auditor"


# ---------------------------------------------------------------------------
# §0.3 — EEO auto-decline vs. the legal attestations that must NOT default
# ---------------------------------------------------------------------------
_EEO_FIELDS = ["gender", "hispanic_latino", "race_ethnicity",
               "veteran_status", "disability_status"]
# Everything else that is gated. NONE of these may ever be auto-defaulted.
_NEVER_DEFAULT = sorted(qb.gated_field_ids() - set(_EEO_FIELDS))


class TestEeoDeclineVsLegalAttestations:
    @pytest.mark.parametrize("field_id", _EEO_FIELDS)
    def test_protected_eeo_auto_declines(self, db, field_id):
        out = lib.resolve(USER, [_nq(field_id)])
        hit = out["resolved"][field_id]
        assert hit["source"] == lib.SOURCE_POLICY_DECLINE
        assert hit["review_required"] is True
        assert qb._option_concept(qb._norm(str(hit["value"]))) == "decline"
        assert out["has_unconfirmed_legal_attestation"] is False

    @pytest.mark.parametrize("field_id", _NEVER_DEFAULT)
    def test_no_other_gated_field_is_ever_auto_defaulted(self, db, field_id):
        """Spec §0 rule 1 — work authorization, sponsorship, citizenship, export
        control, age, criminal history, consents: undeclared means ASK."""
        out = lib.resolve(USER, [_nq(field_id)])
        assert out["resolved"] == {}, f"{field_id} was auto-defaulted"
        assert field_id in out["unresolved"]
        assert out["has_unconfirmed_legal_attestation"] is True

    def test_work_auth_is_not_defaulted_even_alongside_eeo(self, db):
        out = lib.resolve(USER, [_nq("gender"), _nq("work_authorized_us")])
        assert "gender" in out["resolved"]
        assert "work_authorized_us" not in out["resolved"]
        assert out["has_unconfirmed_legal_attestation"] is True
        assert out["coverage"] == pytest.approx(0.5)

    def test_declared_eeo_beats_the_policy_default(self, db):
        db.user_preferences.insert_one({"user_email": USER, "gender": "Female"})
        hit = lib.resolve(USER, [_nq("gender")])["resolved"]["gender"]
        assert hit["value"] == "Female"
        assert hit["source"] == lib.SOURCE_DECLARED

    def test_declared_sources_set_covers_every_emitted_source(self, db):
        """`auto_apply_service._DECLARED_SOURCES` must be a superset of this, or a
        legitimately-resolved answer is bounced back to needs_input."""
        assert lib.SOURCE_POLICY_DECLINE in lib.DECLARED_SOURCES
        assert lib.SOURCE_RESUME_MATCH in lib.DECLARED_SOURCES
        assert lib.SOURCE_DECLARED in lib.DECLARED_SOURCES
        assert lib.SOURCE_PROFILE in lib.DECLARED_SOURCES
        assert lib.SOURCE_USER_STORE in lib.DECLARED_SOURCES


# ---------------------------------------------------------------------------
# §3 — remember() / remember_many() so the same question is never asked twice
# ---------------------------------------------------------------------------
class TestRememberContract:
    def test_remember_by_field_id_resolves_next_time(self, db):
        assert lib.remember(USER, field_id="notice_period", answer="3 weeks")
        out = lib.resolve(USER, [_nq("notice_period")])
        hit = out["resolved"]["notice_period"]
        assert hit["value"] == "3 weeks"
        assert hit["source"] == lib.SOURCE_USER_STORE
        assert hit["review_required"] is True

    def test_remember_by_field_id_matches_a_different_phrasing(self, db):
        """The whole point: the SAME question worded differently by another ATS
        still resolves from the store."""
        lib.remember(USER, "How much notice must you give?",
                     "3 weeks", field_id="notice_period")
        out = lib.resolve(USER, [_nq(
            "notice_period", label="What notice period are you required to serve?")])
        assert out["resolved"]["notice_period"]["value"] == "3 weeks"

    def test_remembered_legal_attestation_is_reused(self, db):
        """A work-auth answer the user gave once is THEIR declared value — it
        must not be asked on every subsequent application."""
        before = lib.resolve(USER, [_nq("work_authorized_us")])
        assert before["has_unconfirmed_legal_attestation"] is True

        lib.remember(USER, field_id="work_authorized_us", answer="Yes")
        after = lib.resolve(USER, [_nq("work_authorized_us")])
        hit = after["resolved"]["work_authorized_us"]
        assert hit["value"] == "Yes"
        assert hit["source"] == lib.SOURCE_USER_STORE
        assert hit["review_required"] is True
        assert after["has_unconfirmed_legal_attestation"] is False

    def test_unknown_field_id_falls_back_to_the_label(self, db):
        stored = lib.remember(USER, "Zorp glimber wux plonk?", "42",
                              field_id="not_a_real_field")
        assert stored is not None and stored["field_id"] is None

    def test_essay_field_id_is_not_banked(self, db):
        assert lib.remember(USER, answer="Because I love it",
                            field_id="why_company") is None
        assert lib.remember(USER, answer="...", field_id="cover_letter") is None

    def test_empty_input_is_none(self, db):
        assert lib.remember(USER, "", None) is None
        assert lib.remember("", "q", "a") is None
        assert lib.remember(USER, "q", None) is None

    def test_remember_stays_user_scoped(self, db):
        lib.remember(USER, field_id="notice_period", answer="3 weeks")
        out = lib.resolve(OTHER, [_nq("notice_period")])
        assert "notice_period" in out["unresolved"]


class TestRememberMany:
    def test_bulk_remember_and_declare(self, db):
        res = lib.remember_many(USER, {
            "notice_period": "3 weeks",
            "willing_to_relocate": True,
            "Zorp glimber wux plonk?": "42",
        })
        assert set(res["remembered"]) == {
            "notice_period", "willing_to_relocate", "Zorp glimber wux plonk?"}
        # catalog fields ALSO land in the declared store -> source "declared".
        assert set(res["declared"]) == {"notice_period", "willing_to_relocate"}
        prefs = db.user_preferences.find_one({"user_email": USER})
        assert prefs["notice_period"] == "3 weeks"
        assert prefs["willing_to_relocate"] is True

        out = lib.resolve(USER, [_nq("notice_period"), _nq("willing_to_relocate")])
        assert out["resolved"]["notice_period"]["source"] == lib.SOURCE_DECLARED
        assert out["unresolved"] == []

    def test_declare_false_keeps_prefs_untouched(self, db):
        lib.remember_many(USER, {"notice_period": "3 weeks"}, declare=False)
        assert db.user_preferences.find_one({"user_email": USER}) is None
        out = lib.resolve(USER, [_nq("notice_period")])
        assert out["resolved"]["notice_period"]["source"] == lib.SOURCE_USER_STORE

    def test_one_bad_value_does_not_discard_the_rest(self, db):
        res = lib.remember_many(USER, {
            "notice_period": "3 weeks",
            "gender": "Definitely not an option on this form",
        })
        assert "notice_period" in res["declared"]
        assert res["skipped"].get("gender") == "invalid_value"
        assert db.user_preferences.find_one(
            {"user_email": USER})["notice_period"] == "3 weeks"

    def test_bad_input_is_total_not_raising(self, db):
        assert lib.remember_many(USER, "not a dict") == {
            "remembered": [], "declared": [], "skipped": {}}
        assert lib.remember_many("", {"a": 1})["remembered"] == []


# ---------------------------------------------------------------------------
# §7 — needs_generation carries the raw question for the grounded generator
# ---------------------------------------------------------------------------
class TestNeedsGenerationShape:
    def test_free_text_items_carry_the_raw_label(self, db):
        out = lib.resolve(USER, [_nq(
            "why_company", label="Why do you want to work at Acme?")])
        item = out["needs_generation"][0]
        assert item["field_id"] == "why_company"
        assert item["question"] == "Why do you want to work at Acme?"
        assert item["kind"] == lib.KIND_ESSAY
        # never answered here.
        assert out["resolved"] == {}

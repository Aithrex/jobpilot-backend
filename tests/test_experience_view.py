"""The job board opens on the candidate's OWN level.

Until 2026-10-02 the "For you" board defaulted to every level, and every
current user is early-career (0-4 years): a zero-year user's first screen held
"Web Application Architect 3", "Software Engineer III" and two executive
postings. `experience_view` is the default the board now opens on - the
user can widen it to all levels in one tap.

Cases are the real (anonymised) users of that audit.
"""
from services import experience_view as ev


def levels(pref=None, years=None):
    return ev.resolve(pref, years)["levels"]


class TestResolve:
    def test_junior_with_one_year_is_entry(self):
        assert levels("Junior", 1) == ["entry"]

    def test_stated_and_resume_disagree_show_both(self):
        # Told us "Junior", resume says three years: both bands.
        assert levels("Junior", 3) == ["entry", "mid"]

    def test_a_band_edge_shows_the_next_band_too(self):
        assert levels("Junior", 2) == ["entry", "mid"]
        assert levels("Mid-level", 4) == ["mid", "senior"]

    def test_new_grad_includes_internships(self):
        assert levels("Intern / New grad", 1) == ["intern", "entry"]

    def test_zero_years_and_no_preference_is_intern_and_entry(self):
        assert levels(None, 0) == ["intern", "entry"]

    def test_the_stated_level_wins_over_an_inflated_resume_title(self):
        # Three years, "Senior ..." on the resume, told us "Mid": mid, not senior.
        assert levels("Mid", 3) == ["mid"]

    def test_staff_and_above_is_its_own_view(self):
        assert levels("Lead / Staff", 9) == ["staff"]
        assert levels("Manager / Director", None) == ["staff"]
        assert levels("Senior", 6) == ["senior"]
        assert levels("Senior", 7) == ["senior", "staff"]

    def test_nothing_known_is_all_levels(self):
        out = ev.resolve(None, None)
        assert out["levels"] == []
        assert out["source"] == "none"

    def test_the_source_is_reported(self):
        assert ev.resolve("Junior", 1)["source"] == "preference+resume"
        assert ev.resolve("Junior", None)["source"] == "preference"
        assert ev.resolve(None, 1)["source"] == "resume"

    def test_garbage_preference_is_ignored(self):
        assert levels("banana", 1) == ["entry"]


class TestForUser:
    def test_reads_the_preference_and_the_active_resume(self, mock_db):
        for c in ("users", "user_preferences", "resume_assets"):
            mock_db[c].delete_many({})
        mock_db.users.insert_one({"email": "a@b.com", "active_resume_id": "r1"})
        mock_db.user_preferences.insert_one({"user_email": "a@b.com", "experience_level": "Junior"})
        mock_db.resume_assets.insert_one({"resume_id": "r1", "experience_years": 3,
                                          "parse_status": "parsed"})
        out = ev.for_user("a@b.com")
        assert out["levels"] == ["entry", "mid"]
        assert out["years"] == 3

    def test_unknown_user_is_all_levels_not_an_error(self, mock_db):
        assert ev.for_user("nobody@b.com")["levels"] == []


class TestTheBoardReceivesIt:
    """`GET /api/jobs/title-seed` - the call the board already makes on load -
    carries the view, so no new edge route is needed."""

    def test_title_seed_carries_the_experience_view(self, mock_db):
        from _vg_support import load_service_app
        from flask_jwt_extended import create_access_token
        for c in ("users", "user_preferences", "resume_assets"):
            mock_db[c].delete_many({})
        mock_db.users.insert_one({"email": "seed@b.com", "active_resume_id": "r9"})
        mock_db.user_preferences.insert_one({"user_email": "seed@b.com",
                                             "experience_level": "Junior"})
        mock_db.resume_assets.insert_one({"resume_id": "r9", "experience_years": 1,
                                          "parse_status": "parsed"})
        app = load_service_app("jobs")
        with app.app_context():
            token = create_access_token(identity="seed@b.com")
        resp = app.test_client().get("/api/jobs/title-seed",
                                     headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200, resp.get_json()
        view = resp.get_json()["experience_view"]
        assert view["levels"] == ["entry"]
        assert view["source"] == "preference+resume"

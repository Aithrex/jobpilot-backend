"""Guest role-term derivation must never guess a career.

The old `_guest_role_terms` read only experience[].title and fell back to
["software engineer"]. Any resume whose roles did not land in that one field -
a nurse, an accountant, a teacher, a new grad - triggered a full ~1000-tenant
software-engineering scan, whose results were then scored against non-tech
skills. The guest saw near-zero matches and an empty_reason that blamed the
market for our own bad guess.

These tests pin the two halves of the fix: evidence is widened, and when there
is genuinely no evidence we say so instead of inventing a target role.
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared", "python"))

from services import guest_service  # noqa: E402


class TestRoleTermDerivation:
    def test_experience_titles_are_preferred(self):
        terms = guest_service._guest_role_terms({
            "experience": [
                {"title": "Registered Nurse"},
                {"title": "Charge Nurse"},
            ],
        })
        assert terms == ["Registered Nurse", "Charge Nurse"]

    def test_caps_at_three_terms(self):
        terms = guest_service._guest_role_terms({
            "experience": [{"title": f"Role {i}"} for i in range(10)],
        })
        assert len(terms) == 3

    def test_duplicate_titles_are_collapsed_case_insensitively(self):
        terms = guest_service._guest_role_terms({
            "experience": [
                {"title": "Staff Accountant"},
                {"title": "staff accountant"},
                {"title": "Senior Accountant"},
            ],
        })
        assert terms == ["Staff Accountant", "Senior Accountant"]

    def test_falls_back_to_declared_headline_when_no_experience(self):
        """Student and career-change resumes often have an empty experience[]
        but do declare the role they are targeting."""
        terms = guest_service._guest_role_terms({
            "experience": [],
            "headline_title": "Paralegal",
        })
        assert terms == ["Paralegal"]

    def test_falls_back_to_field_of_study_as_last_resort(self):
        terms = guest_service._guest_role_terms({
            "experience": [],
            "education": [{"degree": "Bachelor of Science in Nursing"}],
        })
        assert terms == ["Bachelor of Science in Nursing"]

    def test_field_of_study_stops_at_one_term(self):
        """A degree names a domain, not a job. Weak evidence stays narrow."""
        terms = guest_service._guest_role_terms({
            "experience": [],
            "education": [
                {"degree": "BSc Nursing"},
                {"degree": "MSc Public Health"},
            ],
        })
        assert len(terms) == 1


class TestNoSoftwareEngineerFallback:
    def test_empty_resume_yields_no_terms_not_software_engineer(self):
        assert guest_service._guest_role_terms({}) == []

    def test_nontechnical_resume_never_derives_software_engineering(self):
        terms = guest_service._guest_role_terms({
            "experience": [{"title": "Registered Nurse"}],
            "skills": ["Patient care", "Triage", "IV therapy"],
        })
        joined = " ".join(terms).lower()
        assert "software" not in joined
        assert "engineer" not in joined
        assert "developer" not in joined

    def test_blank_titles_do_not_resurrect_the_default(self):
        """Whitespace-only titles must not count as evidence, and must not
        trip the old `terms or [...]` fallback either."""
        terms = guest_service._guest_role_terms({
            "experience": [{"title": "   "}, {"title": ""}, {"title": None}],
        })
        assert terms == []


class TestScanIsSkippedWhenRoleIsUndetermined:
    def test_fetch_returns_role_undetermined_without_scanning(self, monkeypatch):
        """No role evidence must skip the fan-out entirely. Burning the budget
        on a guessed career is worse than an honest empty result."""
        called = []
        monkeypatch.setattr(
            guest_service, "_run_workday",
            lambda *a, **k: called.append("workday"), raising=False,
        )

        rows, report = guest_service._fetch_free_jobs({"experience": []})

        assert rows == []
        assert report["empty_reason"] == "role_undetermined"
        assert report["considered"] == 0
        assert called == [], "no source should be fetched without role evidence"

    def test_scan_still_runs_when_evidence_exists(self, monkeypatch):
        """The guard must not swallow legitimate scans."""
        monkeypatch.setattr(
            guest_service, "_guest_role_terms",
            lambda _s: ["Registered Nurse"],
        )
        seen = {}

        def _fake_pool(*_a, **_k):
            seen["ran"] = True
            raise RuntimeError("stop here - we only need to prove we got past the guard")

        monkeypatch.setattr(
            "concurrent.futures.ThreadPoolExecutor", _fake_pool, raising=False,
        )
        try:
            guest_service._fetch_free_jobs({"experience": [{"title": "Registered Nurse"}]})
        except Exception:
            pass
        assert seen.get("ran") is True

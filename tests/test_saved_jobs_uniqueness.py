"""Defect L regression: saved_jobs dedup must be PER-USER, not global.

The bug: ``JobService._ensure_indexes`` created a GLOBAL unique index on
``job_id`` alone. Because a saved job_id is source-prefixed ("{source}:{id}",
job_service:~1588) it is a per-POSTING canonical id shared across users, so
when user B saved a posting user A had already saved, the upsert filter
{job_id, user_email} matched nothing -> insert -> DuplicateKeyError on the
global unique -> user B could not save the job at all.

The fix: the dedup key is the compound ``(user_email, job_id)`` — idempotent
per user, collision-free across users. These tests pin that behavior.

mongomock 4.3.0 enforces BOTH compound AND single-field unique indexes (verified
in-suite: a global unique on job_id blocks a second user; the compound does not),
so every assertion below exercises real index enforcement, not a stub.
"""

import pytest

from services.job_service import JobService

# Source-prefixed canonical posting id (see job_service:~1588). The SAME posting
# is saved by two different users below — that is the whole point of the defect.
JOB_ID = "linkedin:998877"
USER_A = "alice@example.com"
USER_B = "bob@example.com"


def _posting(job_id=JOB_ID, title="Staff Engineer", company="Acme"):
    return {"job_id": job_id, "title": title, "company": company}


@pytest.fixture()
def svc(mock_db):
    """A JobService bound to the in-memory mock DB with a clean saved_jobs.

    Constructing JobService() runs _ensure_indexes(), which builds the compound
    unique ``user_email_job_id_unique`` on the mock collection — so these tests
    exercise the exact index the runtime creates.
    """
    mock_db["saved_jobs"].delete_many({})
    service = JobService()
    service.saved_jobs.delete_many({})
    return service


class TestPerUserUniqueness:
    def test_two_different_users_save_same_job(self, svc):
        """DEFECT L: user B must be able to save a posting user A already saved."""
        a = svc.save_job(_posting(), user_email=USER_A)
        b = svc.save_job(_posting(), user_email=USER_B)  # would raise pre-fix

        assert a["user_email"] == USER_A
        assert b["user_email"] == USER_B
        assert a["_id"] != b["_id"]
        # Two DISTINCT docs for the same canonical posting.
        assert svc.saved_jobs.count_documents({"job_id": JOB_ID}) == 2

    def test_same_user_saving_twice_is_idempotent(self, svc):
        """Re-saving is an upsert on (user_email, job_id): one doc, no dup error."""
        first = svc.save_job(_posting(title="Original"), user_email=USER_A)
        second = svc.save_job(_posting(title="Edited"), user_email=USER_A)

        assert svc.saved_jobs.count_documents(
            {"job_id": JOB_ID, "user_email": USER_A}
        ) == 1
        # Same logical row, updated in place.
        assert first["_id"] == second["_id"]
        assert second["job_data"]["title"] == "Edited"

    def test_delete_is_user_scoped(self, svc):
        """Deleting A's save must not touch B's save of the same posting."""
        svc.save_job(_posting(), user_email=USER_A)
        svc.save_job(_posting(), user_email=USER_B)

        assert svc.delete_saved_job(JOB_ID, user_email=USER_A) is True

        assert svc.saved_jobs.count_documents(
            {"job_id": JOB_ID, "user_email": USER_A}
        ) == 0
        # B's save is untouched.
        assert svc.saved_jobs.count_documents(
            {"job_id": JOB_ID, "user_email": USER_B}
        ) == 1

    def test_get_saved_jobs_returns_only_callers_saves(self, svc):
        """The {user_email} read path is scoped — A never sees B's saves."""
        svc.save_job(_posting(job_id="linkedin:1"), user_email=USER_A)
        svc.save_job(_posting(job_id="linkedin:2"), user_email=USER_A)
        svc.save_job(_posting(job_id="linkedin:2"), user_email=USER_B)  # shared posting

        a_jobs = svc.get_saved_jobs(USER_A)
        b_jobs = svc.get_saved_jobs(USER_B)

        assert {j["job_id"] for j in a_jobs} == {"linkedin:1", "linkedin:2"}
        assert all(j["user_email"] == USER_A for j in a_jobs)
        assert {j["job_id"] for j in b_jobs} == {"linkedin:2"}
        assert all(j["user_email"] == USER_B for j in b_jobs)


class TestIndexShape:
    def test_compound_unique_index_present_global_absent(self, svc):
        """The dedup index is the compound (user_email, job_id) — NOT a global
        unique on job_id. A stray global job_id unique would re-introduce defect L."""
        info = svc.saved_jobs.index_information()
        assert "user_email_job_id_unique" in info
        idx = info["user_email_job_id_unique"]
        assert idx.get("unique") is True
        assert [k for k, _ in idx["key"]] == ["user_email", "job_id"]

        # No single-field unique on job_id alone (the defect-L index) is created.
        for name, spec in info.items():
            keys = [k for k, _ in spec["key"]]
            if keys == ["job_id"]:
                assert not spec.get("unique"), (
                    f"index {name} is a GLOBAL unique on job_id — reintroduces defect L"
                )

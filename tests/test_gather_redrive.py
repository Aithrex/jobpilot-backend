"""A row whose gather invocation was lost must not be orphaned forever.

`apply_match_sweep` and `POST /api/apply/queue` create the ledger row and then
fire `auto_apply_gather` as an async Lambda self-invoke. That invoke is
best-effort — throttled, dropped, or killed with its Lambda — and nothing re-drove
`queued`: step 1 of the hourly sweep only looks at the SUBMIT-pickup state.

Observed in production: of 10 rows one sweep tick created, 4 gathered and 6 were
still `queued` half an hour later, each with a single `application_created` audit
line and no answers document. `queued` is not in any fold bucket either, so the
user could not even see them — the application had simply evaporated.
"""
import services.apply_scheduler as sched
import services.application_ledger_service as als


class _Svc:
    def __init__(self):
        self.jobs = []

    def create_job(self, job_type, payload, user_email=None):
        self.jobs.append((job_type, payload.get("application_id")))
        return f"job-{len(self.jobs)}"


def _rows(*specs):
    return [{"application_id": aid, "apply_url": f"https://x/{aid}",
             "posting_closes_at": closes} for aid, closes in specs]


def _patch(monkeypatch, svc, by_status):
    """`_run_user_hourly` imports `als` / `get_resume_service` INSIDE the
    function body, so the module attributes on `apply_scheduler` do not exist —
    patch the source modules instead."""
    import services.resume_service as rs
    monkeypatch.setattr(rs, "get_resume_service", lambda: svc)
    monkeypatch.setattr(rs.ResumeService, "invoke_async",
                        staticmethod(lambda *a, **k: None))
    monkeypatch.setattr(
        als, "list_applications",
        lambda email, status=None, limit=None: by_status.get(status, []))


class TestUngatheredRowsAreRedriven:
    def test_a_queued_row_gets_gather_re_enqueued(self, monkeypatch):
        svc = _Svc()
        _patch(monkeypatch, svc, {als.STATUS_QUEUED: _rows(("a1", None))})
        sched._run_user_hourly("u@x.co")
        assert ("auto_apply_gather", "a1") in svc.jobs

    def test_a_stalled_gathering_row_is_also_redriven(self, monkeypatch):
        """`gathering` is the same hole one step later — the row transitioned
        and then its worker died."""
        svc = _Svc()
        _patch(monkeypatch, svc, {als.STATUS_GATHERING: _rows(("a2", None))})
        sched._run_user_hourly("u@x.co")
        assert ("auto_apply_gather", "a2") in svc.jobs

    def test_it_is_counted(self, monkeypatch):
        svc = _Svc()
        _patch(monkeypatch, svc, {als.STATUS_QUEUED: _rows(("a1", None), ("a2", None))})
        out = sched._run_user_hourly("u@x.co")
        assert out["gather_redriven"] == 2


class TestSafety:
    def test_a_closed_posting_is_never_redriven(self, monkeypatch):
        """Re-driving into a dead posting burns an application slot and a
        browser run for a job nobody can apply to."""
        import datetime
        past = datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc)
        svc = _Svc()
        _patch(monkeypatch, svc, {als.STATUS_QUEUED: _rows(("dead", past))})
        out = sched._run_user_hourly("u@x.co")
        assert ("auto_apply_gather", "dead") not in svc.jobs
        assert out["skipped_closed"] >= 1

    def test_redrive_never_enqueues_a_submit(self, monkeypatch):
        """This path must only ever re-run GATHER. Submitting a row that never
        gathered would send a form we never filled."""
        svc = _Svc()
        _patch(monkeypatch, svc, {als.STATUS_QUEUED: _rows(("a1", None))})
        sched._run_user_hourly("u@x.co")
        assert not [j for j in svc.jobs
                    if j[0] == "auto_apply_submit" and j[1] == "a1"]

    def test_a_listing_failure_does_not_abort_the_sweep(self, monkeypatch):
        import services.resume_service as rs
        svc = _Svc()
        monkeypatch.setattr(rs, "get_resume_service", lambda: svc)
        monkeypatch.setattr(rs.ResumeService, "invoke_async",
                            staticmethod(lambda *a, **k: None))

        def boom(email, status=None, limit=None):
            if status == als.STATUS_QUEUED:
                raise RuntimeError("mongo down")
            return []

        monkeypatch.setattr(als, "list_applications", boom)
        out = sched._run_user_hourly("u@x.co")   # must not raise
        assert out["gather_redriven"] == 0

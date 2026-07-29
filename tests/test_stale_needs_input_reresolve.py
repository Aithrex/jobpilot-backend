"""A parked row must be re-resolved when the declared profile catches up.

`needs_input` freezes the questions that were unresolvable at GATHER time and
nothing ever revisits them. So completing onboarding — or answering the same
question on some other application — does not release the rows already waiting
on exactly that fact.

Observed in production: two rows (EnergyHub, Adyen) parked on "Country" at
19:09, four minutes BEFORE the user's onboarding wrote `country`. Both then sat
asking the user for a value the platform already held, and `country` resolves at
coverage 1.0 in every recon shape.

The hourly sweep now re-resolves each parked row with an EMPTY answer map, which
is exactly "re-resolve what is still open, promote only if nothing is left".
"""
import services.apply_scheduler as sched
import services.application_ledger_service as als
import services.auto_apply_service as aas


class _Svc:
    def __init__(self):
        self.jobs = []

    def create_job(self, job_type, payload, user_email=None):
        self.jobs.append((job_type, payload.get("application_id")))
        return f"job-{len(self.jobs)}"


def _parked(*specs):
    return [{"application_id": aid, "apply_url": f"https://x/{aid}",
             "posting_closes_at": closes} for aid, closes in specs]


def _patch(monkeypatch, svc, by_status, resume_impl=None):
    """`_run_user_hourly` imports its collaborators INSIDE the function body,
    so the source modules are what must be patched."""
    import services.resume_service as rs
    monkeypatch.setattr(rs, "get_resume_service", lambda: svc)
    monkeypatch.setattr(rs.ResumeService, "invoke_async",
                        staticmethod(lambda *a, **k: None))
    monkeypatch.setattr(
        als, "list_applications",
        lambda email, status=None, limit=None: by_status.get(status, []))
    calls = []

    def _default(email, aid, answers):
        calls.append((email, aid, answers))
        return {"status": "resumed",
                "application_status": als.STATUS_NEEDS_INPUT}

    monkeypatch.setattr(aas, "resume_after_answers", resume_impl or _default)
    return calls


class TestStaleRowsAreReResolved:
    def test_each_parked_row_is_re_resolved(self, monkeypatch):
        svc = _Svc()
        calls = _patch(monkeypatch, svc,
                       {als.STATUS_NEEDS_INPUT: _parked(("a1", None),
                                                        ("a2", None))})
        sched._run_user_hourly("u@x.co")
        assert [c[1] for c in calls] == ["a1", "a2"]

    def test_it_submits_no_answers_of_its_own(self, monkeypatch):
        """The sweep must never invent an answer — it only asks the resolver to
        look again at what the user has already declared."""
        svc = _Svc()
        calls = _patch(monkeypatch, svc,
                       {als.STATUS_NEEDS_INPUT: _parked(("a1", None))})
        sched._run_user_hourly("u@x.co")
        assert calls[0][2] == {}

    def test_a_row_that_clears_is_counted(self, monkeypatch):
        svc = _Svc()

        def _clears(email, aid, answers):
            return {"status": "resumed",
                    "application_status": als.STATUS_APPROVED}

        _patch(monkeypatch, svc,
               {als.STATUS_NEEDS_INPUT: _parked(("a1", None))},
               resume_impl=_clears)
        out = sched._run_user_hourly("u@x.co")
        assert out["reresolved"] == 1

    def test_a_row_still_blocked_is_not_counted(self, monkeypatch):
        """A CAPTCHA/account wall comes back unchanged — that is not progress
        and must not be reported as if the row moved."""
        svc = _Svc()
        _patch(monkeypatch, svc,
               {als.STATUS_NEEDS_INPUT: _parked(("wall", None))})
        out = sched._run_user_hourly("u@x.co")
        assert out["reresolved"] == 0


class TestSafety:
    def test_a_closed_posting_is_never_re_resolved(self, monkeypatch):
        import datetime
        past = datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc)
        svc = _Svc()
        calls = _patch(monkeypatch, svc,
                       {als.STATUS_NEEDS_INPUT: _parked(("dead", past))})
        out = sched._run_user_hourly("u@x.co")
        assert calls == []
        assert out["skipped_closed"] >= 1

    def test_one_row_failing_does_not_abort_the_others(self, monkeypatch):
        svc = _Svc()
        seen = []

        def _boom(email, aid, answers):
            seen.append(aid)
            if aid == "bad":
                raise RuntimeError("resolver exploded")
            return {"status": "resumed",
                    "application_status": als.STATUS_NEEDS_INPUT}

        _patch(monkeypatch, svc,
               {als.STATUS_NEEDS_INPUT: _parked(("bad", None), ("good", None))},
               resume_impl=_boom)
        sched._run_user_hourly("u@x.co")
        assert seen == ["bad", "good"]

    def test_a_listing_failure_does_not_abort_the_sweep(self, monkeypatch):
        import services.resume_service as rs
        svc = _Svc()
        monkeypatch.setattr(rs, "get_resume_service", lambda: svc)
        monkeypatch.setattr(rs.ResumeService, "invoke_async",
                            staticmethod(lambda *a, **k: None))

        def boom(email, status=None, limit=None):
            if status == als.STATUS_NEEDS_INPUT:
                raise RuntimeError("mongo down")
            return []

        monkeypatch.setattr(als, "list_applications", boom)
        out = sched._run_user_hourly("u@x.co")
        assert out["reresolved"] == 0

# Scheduled jobs → the resume Lambda's scheduled-event branch
# (event {"scheduled_job": "<name>"} handled in services/resume/lambda_handler.py
# → services.scheduled_jobs.dispatch_scheduled). EventBridge scheduled-rule
# invocations are free; the Lambda runs sit inside the always-free tier.
#
# - digest_hourly : fires every hour; the dispatcher sends the morning digest to
#   users at THEIR local 7am and the evening summary at their local 7pm
#   (opt-in only). Morning enqueues a free (Apify-excluded) daily_pipeline run.
# - bandit_nightly: nightly signal-bandit posterior update.
# - learning_due  : sweep due scheduled LinkedIn posts every 10 minutes.

locals {
  scheduled_jobs = {
    digest_hourly  = { expression = "rate(1 hour)", desc = "Hourly digest fan-out (per-user local 7am/7pm)" }
    bandit_nightly = { expression = "cron(0 3 * * ? *)", desc = "Nightly signal-bandit update (03:00 UTC)" }
    learning_due   = { expression = "rate(10 minutes)", desc = "Send due scheduled LinkedIn posts" }
    events_refresh = { expression = "cron(0 6 * * ? *)", desc = "Refresh hackathon/webinar catalog (Devpost + confs.tech, 06:00 UTC)" }
    # DRAFT — human review. Hourly auto-apply sweep: enqueue eligible applications,
    # send the evening summary (self-filtered by local hour), reconcile Oracle
    # verify-freezes. Routes to scheduled_jobs.dispatch_scheduled("apply_hourly").
    apply_hourly = { expression = "rate(1 hour)", desc = "Auto-apply hourly sweep + evening summary + verify re-check", state = "DISABLED" }
    # DRAFT — human review. Spec §5: the NEW-POSTING auto-apply fan-out.
    # `apply_hourly` above only ever considered jobs the user had already SAVED
    # by hand, so the user was still the discovery step. This tick joins new
    # inventory to per-user auto-apply: opted-in user -> active résumé ->
    # postings first_seen since their watermark -> fit_score >= match_threshold
    # (65, the "good match" band — NOT autonomy_threshold, which is
    # form-fillability) -> dedup gate -> ledger row -> auto_apply_gather.
    #
    # Offset to minute 10 of each quarter hour so it lands BETWEEN the two
    # ingest fan-outs (0/15 Workday, 7/15 career pages) rather than competing
    # with them for Lambda concurrency, and so it reads inventory the tick
    # before it has just written.
    #
    # This rule needs BOTH this entry and the matching
    # `scheduled_jobs.dispatch_scheduled("apply_match_sweep")` branch — the
    # `ingest_*` prefix branch does not cover it.
    #
    # Latency reality check: end-to-end "posted -> applied" is bounded by the
    # CRAWL, not by this cadence. The ingest rules are per-source and EACH gets
    # its own 16-shard budget per tick (the "112 / 16 = 7 ticks = ~1h45m" note
    # above conflates them), so a full revisit is:
    #     workday       48 shards / 16 = 3 ticks x 15 min = ~45 min
    #     career-pages  64 shards / 16 = 4 ticks x 15 min = ~60 min
    # degrading toward the ~1h45m figure when shards are in backoff or a lease
    # overruns its 15-minute TTL.
    # So discovery is 0-60 min (mean ~30) and this sweep adds 0-15 min (mean
    # ~7.5): typical posted->applied is ~35-40 min, worst case ~75 min. A 6am
    # posting is applied to well before a 9am wake-up, with hours of headroom.
    # Making THIS tick faster barely moves that number. Landing in a hot
    # posting's first ~100 applicants needs a HOT-SHARD TIER (a handful of
    # high-value employers on their own 1-2 minute rule), not a faster sweep.
    apply_match_sweep = { expression = "cron(10/15 * * * ? *)", desc = "New-posting auto-apply fan-out (fit-gated, per-user watermark)", state = "DISABLED" }
    # DRAFT — human review. Nightly cross-user skill-market recompute. Option (a)
    # is wired: scheduled_jobs.dispatch_scheduled("trend_aggregate") enqueues the
    # resume `_process_job` job of the same name (system actor, payload {}).
    trend_aggregate = { expression = "cron(0 4 * * ? *)", desc = "Nightly cross-user market trend recompute (04:00 UTC)" }
    # Nightly cross-user (role, keyword) -> mean-ATS-lift recompute (Part B3).
    # Aggregate-only output (jd_keyword_ats_stats); same system-actor bridge as
    # trend_aggregate: dispatch_scheduled("ats_signal_aggregate"). Runs after the
    # trend recompute so the two nightly aggregates don't overlap.
    ats_signal_aggregate = { expression = "cron(30 4 * * ? *)", desc = "Nightly cross-user ATS keyword-signal recompute (04:30 UTC)" }
    # Guest Onboarding v2 — daily claim-aware retention sweep. Routes to
    # scheduled_jobs.dispatch_scheduled("guest_retention"), which enqueues the
    # resume `guest_retention_cleanup` job (deletes ONLY expired UNCLAIMED
    # guest data per the admin `retention_days` knob; 0 = keep forever).
    guest_retention = { expression = "cron(45 5 * * ? *)", desc = "Daily guest retention cleanup (05:45 UTC)" }
    # Canonical job inventory (ADR-003).
    # Deliberately EventBridge and NOT SQS: infra cost must not increase, and
    # additional rules against the already-deployed resume Lambda are free.
    # Bounded concurrency, leases, watermarks and idempotency live in MongoDB
    # (job_source_state) instead of queue semantics.
    #
    # These are DISPATCH ticks, not one rule per shard. The registries are
    # partitioned into 48 Workday shards (1009 distinct tenants, ~21 each) and
    # 64 career-page shards (1309 companies, ~20 each). That is 112 shards,
    # which would otherwise be 112 rules. Instead each tick routes to
    # scheduled_jobs.dispatch_scheduled("ingest_*") -> the resume
    # `job_ingest_dispatch` job, which claims at most
    # JOB_INGEST_SHARDS_PER_TICK (16) shards that are neither leased nor in
    # backoff and self-invokes one `job_ingest_shard` job per shard. Adding or
    # resizing a shard is then a code/env change, not a Terraform change.
    #
    # Coverage: each source is dispatched independently, <=16 shards per tick,
    # one tick per 15 minutes. Workday (48) re-crawls in ~45m, career-pages
    # (64) in ~1h, Oracle (48) in ~45m and iCIMS (96) in ~1h30m - all well
    # inside the 7-day guest freshness window (ADR-001). iCIMS is the slowest
    # by construction: one connection at ~2.5s spacing, vendor-wide.
    ingest_workday      = { expression = "cron(0/15 * * * ? *)", desc = "Workday shard ingestion tick (48 shards, <=16/tick)" }
    ingest_career_pages = { expression = "cron(7/15 * * * ? *)", desc = "Career-page shard ingestion tick (64 shards, <=16/tick)" }
    # ADR-004 gave Oracle ORC and iCIMS their own sources, with their own shard
    # counts, precisely because they cost far more per member than a board API
    # does. What it did not do was give them a RULE, so `job_ingest_worker`
    # declared four sources while EventBridge only ever fired two — the code
    # deployed, the tests passed, and `job_source_state` never grew an `oracle`
    # or `icims` row because nothing asked it to. That is the binding
    # constraint on G1/G2, and it is here, not in the providers.
    #
    # Offsets continue the existing 7-minute stagger so four fan-outs never
    # overlap: :00 workday, :07 career-pages, :11 oracle, :04 icims.
    ingest_oracle = { expression = "cron(11/15 * * * ? *)", desc = "Oracle ORC shard ingestion tick (48 shards, <=16/tick)" }
    ingest_icims  = { expression = "cron(4/15 * * * ? *)", desc = "iCIMS shard ingestion tick (96 shards, <=16/tick)", state = "DISABLED" }
    # CUT OVER TO EC2 2026-09-02 (stage 1). The crawl now runs in the
    # `ingest-runner` systemd unit on i-06bf0e03e60d9d409, which schedules
    # icims on the same :04/:19/:34/:49 offsets this rule used. Declared
    # DISABLED rather than only clicked off: `state` is looked up with an
    # ENABLED default, so an undeclared rule silently re-enables itself on
    # the next apply — and both planes crawling is double cost (the Mongo
    # lease keeps it merely wasteful rather than incorrect).
    # To roll back: flip to ENABLED here and drop icims from INGEST_SOURCES.
    # Career-portal discovery: grows the crawlable registry rather than
    # crawling it. A one-off script was never going to drain it — 99.9% of the
    # company registry had never been probed — so it gets a tick.
    #
    # Measured 2026-08-19: 20.8s per company serially, 4.85s at
    # DISCOVERY_CONCURRENCY=8, against 59,673 pending. At 16 workers x 40
    # companies x 4 ticks/hour that is ~2,560 companies/hour, i.e. about a day
    # of continuous running for the current queue — which keeps growing as the
    # registry does.
    #
    # Offset to :02 so it does not fan out in the same minute as any ingest
    # tick; discovery and ingestion both self-invoke Lambdas and share the
    # account's concurrency.
    discover_companies = { expression = "cron(2/15 * * * ? *)", desc = "Career-portal discovery fan-out (16 workers x 40 companies)" }
    # Retention sweep: deletes only postings that are BOTH closed and past
    # purge_at. purge_at is stamped exclusively by close_posting, so a live
    # posting is unreachable from this job (see job_inventory_service:645).
    ingest_maintenance = { expression = "cron(20 5 * * ? *)", desc = "Daily job-inventory retention purge (05:20 UTC)" }
  }
}

resource "aws_cloudwatch_event_rule" "scheduled" {
  for_each = local.scheduled_jobs

  name                = "${var.project}-${each.key}-${var.environment}"
  description         = each.value.desc
  schedule_expression = each.value.expression

  # Rule state is DECLARED, not clicked. The three ingest rules were applied and
  # then disabled out of band, which left the canonical inventory permanently
  # empty (0 postings, 0 shard state, 0 runs) while every read path silently
  # kept using the slow live fan-out. Nothing in the repo said so, and terraform
  # showed no drift because state was not managed here. It is now.
  state = lookup(each.value, "state", "ENABLED")
}

resource "aws_cloudwatch_event_target" "scheduled" {
  for_each = local.scheduled_jobs

  rule  = aws_cloudwatch_event_rule.scheduled[each.key].name
  arn   = aws_lambda_function.service["resume"].arn
  input = jsonencode({ scheduled_job = each.key })
}

resource "aws_lambda_permission" "scheduled" {
  for_each = local.scheduled_jobs

  statement_id  = "AllowEventBridge-${each.key}"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.service["resume"].function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.scheduled[each.key].arn
}

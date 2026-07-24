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
    apply_hourly = { expression = "rate(1 hour)", desc = "Auto-apply hourly sweep + evening summary + verify re-check" }
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
    # Coverage: 112 shards / 16 per tick = 7 ticks. At one tick per 15 minutes
    # the whole registry is re-crawled about every 1h45m, comfortably inside
    # the 7-day guest freshness window (ADR-001). The two sources are offset by
    # 7 minutes so their fan-outs do not overlap.
    ingest_workday      = { expression = "cron(0/15 * * * ? *)", desc = "Workday shard ingestion tick (48 shards, <=16/tick)" }
    ingest_career_pages = { expression = "cron(7/15 * * * ? *)", desc = "Career-page shard ingestion tick (64 shards, <=16/tick)" }
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

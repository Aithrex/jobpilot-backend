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
  }
}

resource "aws_cloudwatch_event_rule" "scheduled" {
  for_each = local.scheduled_jobs

  name                = "${var.project}-${each.key}-${var.environment}"
  description         = each.value.desc
  schedule_expression = each.value.expression
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

# Billing budget + threshold alerts at 0.5 / 0.75 / 0.9 of the monthly budget.
#
# Scoped to THIS project so the plane's spend is isolated from the rest of the
# org. By default the billing account admins + users are emailed at each
# threshold; pass budget_notification_channels to also fan out to Cloud
# Monitoring channels (PagerDuty/Slack/email groups).

resource "google_billing_budget" "apply_executor" {
  billing_account = var.gcp_billing_account
  display_name    = "${var.name_prefix} monthly budget"

  budget_filter {
    projects               = ["projects/${data.google_project.this.number}"]
    calendar_period        = "MONTH"
    credit_types_treatment = "INCLUDE_ALL_CREDITS"
  }

  amount {
    specified_amount {
      currency_code = var.budget_currency
      units         = tostring(var.gcp_monthly_budget)
    }
  }

  threshold_rules {
    threshold_percent = 0.5
    spend_basis       = "CURRENT_SPEND"
  }
  threshold_rules {
    threshold_percent = 0.75
    spend_basis       = "CURRENT_SPEND"
  }
  threshold_rules {
    threshold_percent = 0.9
    spend_basis       = "CURRENT_SPEND"
  }

  all_updates_rule {
    monitoring_notification_channels = var.budget_notification_channels
    disable_default_iam_recipients   = false
    enable_project_level_recipients  = false
  }
}

data "google_project" "this" {
  project_id = var.gcp_project_id
}

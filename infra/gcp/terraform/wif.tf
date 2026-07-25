# Workload Identity Federation — the AWS resume Lambda role federates into GCP
# and enqueues Cloud Tasks with NO static service-account key (the key that would
# otherwise be a long-lived, exfiltratable AWS-side secret).
#
# Trust chain:
#   AWS Lambda (assumes var.aws_lambda_role_arn)
#     -> STS GetCallerIdentity assertion
#     -> WIF provider (accepts ONLY this account + this EXACT role)
#     -> impersonates the enqueuer SA (roles/iam.workloadIdentityUser)
#     -> enqueuer SA has cloudtasks.enqueuer + actAs on the invoker SA.

locals {
  # Role NAME from the ARN (arn:aws:iam::ACCT:role/NAME -> NAME), used in the
  # provider attribute-condition to pin the EXACT assumed-role sessions.
  aws_role_name = element(split("/", var.aws_lambda_role_arn), length(split("/", var.aws_lambda_role_arn)) - 1)
}

resource "google_iam_workload_identity_pool" "aws" {
  workload_identity_pool_id = "${var.name_prefix}-aws"
  display_name              = "JobPilot apply-executor AWS"
  description               = "Federates the AWS resume Lambda role to enqueue Cloud Tasks (no static key)."
}

resource "google_iam_workload_identity_pool_provider" "aws" {
  workload_identity_pool_id          = google_iam_workload_identity_pool.aws.workload_identity_pool_id
  workload_identity_pool_provider_id = "aws-provider"
  display_name                       = "AWS"

  attribute_mapping = {
    "google.subject"        = "assertion.arn"
    "attribute.aws_account" = "assertion.account"
  }

  # Accept ONLY this AWS account AND only assumed-role sessions of the EXACT
  # resume Lambda role. Any other role/account/user in the org is rejected here,
  # before impersonation is even considered.
  attribute_condition = <<-CEL
    assertion.account == "${var.aws_account_id}" &&
    assertion.arn.startsWith("arn:aws:sts::${var.aws_account_id}:assumed-role/${local.aws_role_name}/")
  CEL

  aws {
    account_id = var.aws_account_id
  }
}

# The SA the AWS Lambda impersonates. Least privilege: enqueue tasks + actAs the
# invoker SA (to attach the task's OIDC token). NOTHING else.
resource "google_service_account" "enqueuer" {
  account_id   = "${var.name_prefix}-enqueuer"
  display_name = "JobPilot apply-executor Cloud Tasks enqueuer (AWS-federated)"
}

# Only identities from the trusted AWS account (further gated to the exact role
# by the provider's attribute-condition) may impersonate the enqueuer SA.
resource "google_service_account_iam_member" "aws_can_impersonate_enqueuer" {
  service_account_id = google_service_account.enqueuer.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.aws.name}/attribute.aws_account/${var.aws_account_id}"
}

# Enqueuer may add tasks to THIS queue only (bound at the queue in cloud_tasks.tf)
# and actAs the invoker SA so the task can carry the invoker's OIDC token.
resource "google_service_account_iam_member" "enqueuer_actas_invoker" {
  service_account_id = google_service_account.invoker.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.enqueuer.email}"
}

# Runtime + invoker service accounts (least privilege).

# The identity the Cloud Run worker RUNS AS. It needs almost nothing from GCP —
# it talks to AWS via the short-lived presigned URLs / capability it is handed,
# holds NO durable AWS creds, NO Mongo, NO SSM. Only structured-log write.
resource "google_service_account" "runtime" {
  account_id   = "${var.name_prefix}-runtime"
  display_name = "JobPilot apply-executor Cloud Run runtime"
}

resource "google_project_iam_member" "runtime_log_writer" {
  project = var.gcp_project_id
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${google_service_account.runtime.email}"
}

# The identity whose OIDC token the Cloud Task presents to the PRIVATE Cloud Run
# service. Its ONLY power is invoking that one service (bound in cloud_run.tf).
resource "google_service_account" "invoker" {
  account_id   = "${var.name_prefix}-invoker"
  display_name = "JobPilot apply-executor Cloud Run invoker (OIDC)"
}

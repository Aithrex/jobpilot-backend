# Cloud Tasks queue — the durable, rate-limited buffer between the AWS
# orchestrator and the Cloud Run worker.
#
# DLQ approach: Cloud Tasks has no native dead-letter queue. Poison handling is
# LAYERED instead:
#   * retry_config.max_attempts caps redeliveries; after that the task is dropped.
#   * BUT a dropped task never means a lost application: the worker's very first
#     act is a one-use capability CLAIM against the AWS side, and the TERMINAL
#     outcome is reported back over the callback surface (which drives the
#     ledger). A task that exhausts attempts without a terminal report leaves the
#     ledger row in `running`; the AWS-side sweep re-checks stuck rows and either
#     re-dispatches (capability re-mint) or marks them for human review.
#   * An ambiguous FINAL submit is reported as `submission_unknown` and is NEVER
#     retried (duplicate-application guard) — this is enforced on the AWS side,
#     independent of the queue's retry policy.
# Operationally: alert on queue depth + task-attempt failures (see the runbook).

resource "google_cloud_tasks_queue" "apply_executor" {
  name     = var.name_prefix
  location = var.gcp_region

  rate_limits {
    max_dispatches_per_second = var.tasks_max_dispatches_per_second
    max_concurrent_dispatches = var.tasks_max_concurrent_dispatches
  }

  retry_config {
    max_attempts       = var.tasks_max_attempts
    min_backoff        = "10s"
    max_backoff        = "300s"
    max_doublings      = 3
    max_retry_duration = "1800s"
  }

  # Log a sample of dispatch outcomes for the alerting policy to key off.
  stackdriver_logging_config {
    sampling_ratio = 1.0
  }
}

# The AWS-federated enqueuer SA may add tasks to THIS queue only.
resource "google_cloud_tasks_queue_iam_member" "enqueuer" {
  location = google_cloud_tasks_queue.apply_executor.location
  name     = google_cloud_tasks_queue.apply_executor.name
  role     = "roles/cloudtasks.enqueuer"
  member   = "serviceAccount:${google_service_account.enqueuer.email}"
}

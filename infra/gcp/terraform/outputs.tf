output "wif_provider_resource_name" {
  description = "Full WIF provider resource name — the AWS side sets this as GCP_WORKLOAD_IDENTITY_PROVIDER for the google-auth external_account credential."
  value       = google_iam_workload_identity_pool_provider.aws.name
}

output "wif_pool_name" {
  value       = google_iam_workload_identity_pool.aws.name
  description = "WIF pool resource name."
}

output "enqueuer_service_account_email" {
  description = "SA the AWS Lambda impersonates via WIF (GCP_TASKS_ENQUEUER_SA on the AWS side)."
  value       = google_service_account.enqueuer.email
}

output "invoker_service_account_email" {
  description = "SA whose OIDC token the Cloud Task carries (GCP_TASKS_INVOKER_SA on the AWS side)."
  value       = google_service_account.invoker.email
}

output "cloud_run_worker_url" {
  description = "Private Cloud Run worker URL (CLOUD_RUN_WORKER_URL on the AWS side, with /task appended)."
  value       = google_cloud_run_v2_service.worker.uri
}

output "cloud_tasks_queue_id" {
  description = "Cloud Tasks queue id (GCP_TASKS_QUEUE on the AWS side)."
  value       = google_cloud_tasks_queue.apply_executor.id
}

output "artifact_registry_repo" {
  description = "Artifact Registry Docker repo the worker image is pushed to."
  value       = "${google_artifact_registry_repository.worker.location}-docker.pkg.dev/${var.gcp_project_id}/${google_artifact_registry_repository.worker.repository_id}"
}

# --- Egress lockdown (audit P0-2) ---
output "vpc_network_name" {
  description = "Custom VPC that carries the worker's locked-down egress."
  value       = google_compute_network.worker.name
}

output "vpc_connector_id" {
  description = "Serverless VPC Access connector the Cloud Run service routes ALL egress through. Must exist BEFORE the Cloud Run deploy."
  value       = google_vpc_access_connector.worker.id
}

output "vpc_connector_name" {
  description = "Serverless VPC Access connector name."
  value       = google_vpc_access_connector.worker.name
}

output "nat_router_name" {
  description = "Cloud Router hosting the Cloud NAT that gives the connector its egress path to the public internet."
  value       = google_compute_router.worker.name
}

# Artifact Registry — the Docker repo the Playwright worker image is pushed to.
resource "google_artifact_registry_repository" "worker" {
  location      = var.gcp_region
  repository_id = var.name_prefix
  format        = "DOCKER"
  description   = "JobPilot auto-apply Playwright worker images."

  docker_config {
    immutable_tags = false # CI pushes :prod repeatedly; digests stay immutable.
  }
}

# The Cloud Run runtime SA must be able to PULL the image.
resource "google_artifact_registry_repository_iam_member" "runtime_reader" {
  location   = google_artifact_registry_repository.worker.location
  repository = google_artifact_registry_repository.worker.name
  role       = "roles/artifactregistry.reader"
  member     = "serviceAccount:${google_service_account.runtime.email}"
}

# Private Cloud Run Playwright worker.
#
#   * ingress = INTERNAL ONLY  (not reachable from the public internet; Cloud
#     Tasks reaches it over Google's internal path).
#   * NO unauthenticated access — only the invoker SA (via the task's OIDC token)
#     may invoke it. allUsers is NEVER granted.
#   * concurrency = 1 (one browser per instance), min = 0 (scale to zero when
#     idle → no idle cost), max = var (cost ceiling + per-domain politeness).
#   * 2 vCPU / 4 GiB, 900s timeout.

resource "google_cloud_run_v2_service" "worker" {
  name                = var.name_prefix
  location            = var.gcp_region
  ingress             = "INGRESS_TRAFFIC_INTERNAL_ONLY"
  deletion_protection = false

  template {
    service_account                  = google_service_account.runtime.email
    max_instance_request_concurrency = 1
    timeout                          = "${var.worker_timeout_seconds}s"

    scaling {
      min_instance_count = 0
      max_instance_count = var.worker_max_instances
    }

    # Egress lockdown (audit P0-2): force ALL outbound traffic through the
    # Serverless VPC Access connector, where the egress firewall (network.tf)
    # denies the metadata server + private ranges and allows only the public
    # internet. This is the network backstop for SSRF / metadata exfil.
    vpc_access {
      connector = google_vpc_access_connector.worker.id
      egress    = "ALL_TRAFFIC"
    }

    containers {
      # On the very first apply the image may not exist yet — a placeholder keeps
      # the plan valid; set var.worker_image (or let CI update the revision) once
      # the image is pushed.
      image = var.worker_image != "" ? var.worker_image : "us-docker.pkg.dev/cloudrun/container/hello"

      resources {
        limits = {
          cpu    = var.worker_cpu
          memory = var.worker_memory
        }
        cpu_idle = true # bill CPU only while a request is in flight.
      }

      env {
        name  = "ENVIRONMENT"
        value = "prod"
      }
      # The AWS callback surface base URL the worker calls back to. Set to the
      # prod site's /api/resume/executor base (rides the existing edge route).
      env {
        name  = "EXECUTOR_CALLBACK_BASE_URL"
        value = "https://aspirely.me/api/resume/executor"
      }

      ports {
        container_port = 8080
      }

      startup_probe {
        http_get { path = "/healthz" }
        initial_delay_seconds = 5
        timeout_seconds       = 3
        period_seconds        = 5
        failure_threshold     = 3
      }
    }
  }

  # CI pushes new images + updates the revision; Terraform must not revert.
  lifecycle {
    ignore_changes = [
      template[0].containers[0].image,
      client,
      client_version,
    ]
  }
}

# ONLY the invoker SA may call the service (the task's OIDC identity). No allUsers.
resource "google_cloud_run_v2_service_iam_member" "invoker" {
  location = google_cloud_run_v2_service.worker.location
  name     = google_cloud_run_v2_service.worker.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.invoker.email}"
}

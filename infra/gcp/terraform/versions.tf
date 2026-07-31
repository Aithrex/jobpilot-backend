# GCP browser-execution plane for JobPilot auto-apply (defect P5).
#
# A SEPARATE Terraform stack from infra/terraform/ (which owns the AWS side).
# This stack provisions the GCP half: Workload Identity Federation (so the AWS
# resume Lambda role can enqueue Cloud Tasks with NO static SA key), a private
# Cloud Run Playwright worker, its Cloud Tasks queue, an Artifact Registry repo,
# and a billing budget with threshold alerts.
#
# It references the AWS side ONLY by value (var.aws_account_id +
# var.aws_lambda_role_arn) — there is no cross-cloud remote-state coupling.
#
# APPLY IS MANUAL (no CI wired). See ../cloud-run-worker/README.md and
# docs/ops/gcp-executor-setup.md for the exact human steps.

terraform {
  required_version = ">= 1.5"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
  }

  # Remote state: create the bucket once (see README), then uncomment. Kept
  # commented so `terraform init` works locally for fmt/validate without a
  # pre-existing bucket.
  # backend "gcs" {
  #   bucket = "jobpilot-exec-tfstate"
  #   prefix = "gcp-executor"
  # }
}

provider "google" {
  project = var.gcp_project_id
  region  = var.gcp_region
}

variable "gcp_project_id" {
  type        = string
  description = "GCP project hosting the browser-execution plane (create it first)."
}

variable "gcp_region" {
  type        = string
  default     = "us-east1"
  description = "Region for Cloud Run, Cloud Tasks and Artifact Registry. Keep close to the AWS resume Lambda region (us-east-1) to minimise cross-cloud latency."
}

variable "gcp_billing_account" {
  type        = string
  description = "Billing account id (XXXXXX-XXXXXX-XXXXXX) the budget is scoped to."
}

variable "name_prefix" {
  type        = string
  default     = "jobpilot-apply-executor"
  description = "Name prefix for all resources in this stack."
}

# --- WIF: the AWS identity that may enqueue Cloud Tasks (NO static SA key) ---
variable "aws_account_id" {
  type        = string
  description = "AWS account id the resume Lambda runs in. The WIF provider accepts ONLY this account."

  validation {
    condition     = can(regex("^[0-9]{12}$", var.aws_account_id))
    error_message = "aws_account_id must be a 12-digit AWS account id."
  }
}

variable "aws_lambda_role_arn" {
  type        = string
  description = "Full ARN of the resume Lambda's execution role (arn:aws:iam::ACCOUNT:role/NAME). The WIF provider's attribute-condition pins the EXACT role — no other role in the account is admitted."

  validation {
    condition     = can(regex("^arn:aws:iam::[0-9]{12}:role/.+$", var.aws_lambda_role_arn))
    error_message = "aws_lambda_role_arn must be an arn:aws:iam::ACCOUNT:role/NAME ARN."
  }
}

# --- Egress lockdown network (audit P0-2) ---
variable "vpc_network_name" {
  type        = string
  default     = ""
  description = "Name of the custom VPC that carries the worker's locked-down egress. Empty defaults to <name_prefix>-vpc."
}

variable "connector_cidr" {
  type        = string
  default     = "10.8.0.0/28"
  description = "Dedicated /28 for the Serverless VPC Access connector. MUST be an unused /28 that does not overlap anything the worker legitimately reaches. It sits inside 10.0.0.0/8 (which is otherwise egress-denied); a higher-priority firewall rule allows traffic within this /28 only."

  validation {
    condition     = can(regex("^([0-9]{1,3}\\.){3}[0-9]{1,3}/28$", var.connector_cidr))
    error_message = "connector_cidr must be an IPv4 /28 CIDR (Serverless VPC Access requires a /28)."
  }
}

# --- Cloud Run worker sizing ---
variable "worker_image" {
  type        = string
  default     = ""
  description = "Full Artifact Registry image ref the Cloud Run worker runs (e.g. us-east1-docker.pkg.dev/PROJECT/REPO/worker:prod). Empty on first apply is fine — deploy the image, then set this (or let CI update the revision)."
}

variable "worker_cpu" {
  type        = string
  default     = "2"
  description = "vCPU per worker instance (Chromium wants ~1.5)."
}

variable "worker_memory" {
  type        = string
  default     = "4Gi"
  description = "Memory per worker instance."
}

variable "worker_max_instances" {
  type        = number
  default     = 5
  description = "Max concurrent worker instances (cost ceiling + per-domain politeness cap). concurrency is pinned to 1 (one browser per instance)."
}

variable "worker_timeout_seconds" {
  type        = number
  default     = 900
  description = "Cloud Run request timeout. A form is seconds; headroom for slow multi-step forms."
}

# --- Cloud Tasks queue ---
variable "tasks_max_dispatches_per_second" {
  type        = number
  default     = 5
  description = "Queue dispatch rate ceiling."
}

variable "tasks_max_concurrent_dispatches" {
  type        = number
  default     = 5
  description = "Max in-flight tasks (align with worker_max_instances)."
}

variable "tasks_max_attempts" {
  type        = number
  default     = 3
  description = "Cloud Tasks delivery attempts. NOTE: a task delivery retry only re-runs the worker's CLAIM; the one-use capability makes a duplicate claim a 403 no-op, so a retry can never double-submit. An ambiguous FINAL submit is reported as submission_unknown and is NEVER retried."
}

# --- Billing budget ---
variable "gcp_monthly_budget" {
  type        = number
  default     = 100
  description = "Monthly budget (in budget_currency) for this plane. Threshold alerts fire at 0.5 / 0.75 / 0.9."
}

variable "budget_currency" {
  type        = string
  default     = "USD"
  description = "Currency code for the budget amount."
}

variable "budget_notification_channels" {
  type        = list(string)
  default     = []
  description = "Optional Cloud Monitoring notification channel IDs to also alert on budget thresholds (in addition to the billing account admins/users emailed by default)."
}

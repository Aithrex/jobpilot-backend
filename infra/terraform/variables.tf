variable "project" {
  type        = string
  default     = "jobpilot"
  description = "Project/name prefix for all resources."
}

variable "environment" {
  type        = string
  default     = "prod"
  description = "Deployment environment (prod, staging, ...)."
}

variable "aws_region" {
  type        = string
  default     = "us-east-1"
  description = "Region for Lambda + API Gateway. Keep us-east-1 if CloudFront uses this API as an origin."
}

variable "runtime" {
  type        = string
  default     = "python3.12"
  description = "Lambda Python runtime. Must match the wheels built by scripts/build_layer.sh."
}

variable "artifacts_dir" {
  type        = string
  default     = "../../build"
  description = "Path (relative to this dir) to the build/ output of build_layer.sh + package.sh."
}

variable "ssm_path_prefix" {
  type        = string
  default     = "/jobpilot/prod"
  description = "SSM Parameter Store prefix the services read secrets from when USE_SSM_SECRETS=true."
}

variable "allowed_origins" {
  type        = string
  default     = "https://aspirely.me"
  description = "Comma-separated CORS origins passed to every service as ALLOWED_ORIGINS."
}

variable "bedrock_region" {
  type        = string
  default     = "us-east-1"
  description = "Region for Bedrock InvokeModel (Claude). Passed as BEDROCK_REGION."
}

variable "resume_s3_bucket" {
  type        = string
  default     = ""
  description = "S3 bucket holding résumé files (granted to the IAM role). Empty = skip the S3 statement."
}

variable "log_level" {
  type    = string
  default = "INFO"
}

variable "db_name" {
  type        = string
  default     = "aspirely_db"
  description = "MongoDB database name (DB_NAME env var)."
}

variable "frontend_url" {
  type        = string
  default     = "https://aspirely.me"
  description = "Public site URL (FRONTEND_URL / BACKEND_PUBLIC_URL env vars; same origin)."
}

# Managed Application Mailbox (managed-mailbox-v1). See managed_mailbox.tf.
variable "mail_bucket_name" {
  type        = string
  default     = ""
  description = "Override for the managed-mailbox S3 bucket name. Empty = project-mail-environment-accountid (globally-unique default, mirrors the résumé bucket)."
}

variable "mail_raw_retention_days" {
  type        = number
  default     = 365
  description = "Days to retain raw MIME under mail/raw/ before expiry. Metadata lives in Mongo and is unaffected."
}

# Per-service sizing. resume runs long async LLM work, so it gets the most.
variable "service_config" {
  type = map(object({
    routes  = list(string)
    memory  = number
    timeout = number
  }))
  default = {
    auth = {
      routes  = ["/api/auth", "/api/admin"]
      memory  = 512
      timeout = 30
    }
    jobs = {
      routes  = ["/api/jobs", "/api/apply"]
      memory  = 1024
      timeout = 60
    }
    resume = {
      routes  = ["/api/resume", "/api/tech-chronicle"]
      memory  = 1536
      timeout = 300
    }
    billing = {
      routes  = ["/api/billing"]
      memory  = 256
      timeout = 15
    }
  }
}

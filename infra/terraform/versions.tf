terraform {
  required_version = ">= 1.5"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.40"
    }
  }

  # Remote state in S3 (bucket versioned + SSE + public-access-blocked). Native
  # S3 state locking (use_lockfile) — no DynamoDB table needed on TF >= 1.10.
  # Runs need AWS_PROFILE=aithrex (the default profile's creds are invalid).
  backend "s3" {
    bucket       = "aithrex-tfstate"
    key          = "jobpilot-backend/terraform.tfstate"
    region       = "us-east-1"
    encrypt      = true
    use_lockfile = true
  }
}

provider "aws" {
  region = var.aws_region
  default_tags {
    tags = {
      Project     = var.project
      Environment = var.environment
      ManagedBy   = "terraform"
      Component   = "jobpilot-backend"
    }
  }
}

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

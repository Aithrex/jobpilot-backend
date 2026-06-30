terraform {
  required_version = ">= 1.5"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.40"
    }
  }

  # Configure a remote backend for real use (S3 + DynamoDB lock). Left local
  # here so the skeleton runs out of the box; uncomment + fill in to share state.
  # backend "s3" {
  #   bucket         = "aithrex-tfstate"
  #   key            = "jobpilot-backend/terraform.tfstate"
  #   region         = "us-east-1"
  #   dynamodb_table = "aithrex-tflock"
  #   encrypt        = true
  # }
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

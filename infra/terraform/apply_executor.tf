# Auto-apply browser fleet — a dedicated CONTAINER-image Lambda running
# Playwright + Chromium. Isolated from the four zip Lambdas so the heavy browser
# image never bloats their cold starts. The resume Lambda calls it via
# LambdaInvokeExecutor (services/lambda_invoke_executor.py).
#
# DARK by default: the resume Lambda only routes here when AUTO_APPLY_EXECUTOR=
# remote AND EXECUTOR_REMOTE_ENABLED=true AND the AUTO_SUBMIT_ENABLED master
# switch is on. Applying this file provisions the fleet; it submits nothing until
# those flags flip. See infra/APPLY_EXECUTOR_DEPLOY.md.

variable "apply_executor_image_tag" {
  type        = string
  default     = "prod"
  description = "ECR image tag the executor Lambda runs (CI pushes :prod)."
}

resource "aws_ecr_repository" "apply_executor" {
  name                 = "${var.project}-apply-executor-${var.environment}"
  image_tag_mutability = "MUTABLE"
  image_scanning_configuration {
    scan_on_push = true
  }
}

# Dedicated least-privilege role (NOT the shared lambda_exec role).
resource "aws_iam_role" "apply_executor_exec" {
  name = "${var.project}-apply-executor-${var.environment}"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "apply_executor_basic" {
  role       = aws_iam_role.apply_executor_exec.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "apply_executor_inline" {
  name = "apply-executor-access"
  role = aws_iam_role.apply_executor_exec.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ReadResumeWriteProof"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject"]
        Resource = ["${aws_s3_bucket.resumes.arn}/*"]
      },
      {
        Sid      = "DecryptAtsCredentials" # for create_account (Oracle, Phase 2)
        Effect   = "Allow"
        Action   = ["kms:Decrypt"]
        Resource = [aws_kms_key.credentials.arn]
      }
    ]
  })
}

resource "aws_lambda_function" "apply_executor" {
  function_name = "${var.project}-apply-executor-${var.environment}"
  role          = aws_iam_role.apply_executor_exec.arn
  package_type  = "Image"
  image_uri     = "${aws_ecr_repository.apply_executor.repository_url}:${var.apply_executor_image_tag}"

  memory_size = 2048 # Chromium wants ~1.5 GB
  timeout     = 300  # one form is seconds; headroom for slow multi-step forms

  # Cost ceiling + per-domain politeness cap: at most 5 browsers at once.
  # Enabled 2026-07-12 after the account concurrency limit was raised 10 → 1000
  # (Service Quotas L-B99A9384), leaving ample unreserved headroom.
  reserved_concurrent_executions = 5

  environment {
    variables = {
      ENVIRONMENT      = var.environment
      RESUME_S3_BUCKET = aws_s3_bucket.resumes.bucket
      DB_NAME          = var.db_name
      USE_SSM_SECRETS  = "true"
      SSM_MONGODB_URI  = "${var.ssm_path_prefix}/mongodb-uri"
    }
  }

  tracing_config { mode = "Active" }

  # CI pushes new images + updates the function code; Terraform must not revert.
  lifecycle {
    ignore_changes = [image_uri]
  }

  depends_on = [aws_iam_role_policy_attachment.apply_executor_basic]
}

# Let the resume Lambda's role invoke the executor (LambdaInvokeExecutor).
resource "aws_iam_role_policy" "resume_invoke_executor" {
  name = "invoke-apply-executor"
  role = aws_iam_role.lambda_exec.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["lambda:InvokeFunction"]
      Resource = [aws_lambda_function.apply_executor.arn]
    }]
  })
}

# The CI (GitHub Actions) deployer needs ECR auth + push to build/push the
# apply-executor container image (the four zip Lambdas + layer never needed ECR).
# The CI user is managed outside this stack, so attach ONLY this inline policy by
# name. Applied via CLI 2026-07-12 to unblock the failed deploy run; captured here
# as code (a `terraform apply` adopts it — PutUserPolicy is create-or-replace).
resource "aws_iam_user_policy" "ci_ecr" {
  name = "jobpilot-ci-ecr"
  user = "jobpilot-ci-deployer"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "EcrAuthToken"
        Effect   = "Allow"
        Action   = ["ecr:GetAuthorizationToken"]
        Resource = "*"
      },
      {
        Sid    = "EcrPushApplyExecutor"
        Effect = "Allow"
        Action = [
          "ecr:BatchCheckLayerAvailability",
          "ecr:InitiateLayerUpload",
          "ecr:UploadLayerPart",
          "ecr:CompleteLayerUpload",
          "ecr:PutImage",
          "ecr:BatchGetImage",
          "ecr:GetDownloadUrlForLayer",
        ]
        Resource = aws_ecr_repository.apply_executor.arn
      },
    ]
  })
}

output "apply_executor_ecr_url" {
  value = aws_ecr_repository.apply_executor.repository_url
}

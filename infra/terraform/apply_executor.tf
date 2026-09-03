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

variable "apply_executor_monthly_budget" {
  type        = number
  default     = 50
  description = "USD monthly budget alert for the apply-executor plane (tag-filtered)."
}

variable "apply_executor_log_retention_days" {
  type        = number
  default     = 30
  description = "CloudWatch log retention for the apply-executor Lambda."
}

locals {
  apply_executor_tags = {
    Name       = "${var.project}-apply-executor-${var.environment}"
    Service    = "apply-executor"
    Feature    = "auto-apply"
    Component  = "apply-executor"
    DataClass  = "sensitive"
  }
}

resource "aws_ecr_repository" "apply_executor" {
  name                 = "${var.project}-apply-executor-${var.environment}"
  image_tag_mutability = "MUTABLE"
  image_scanning_configuration {
    scan_on_push = true
  }
  tags = local.apply_executor_tags
}

resource "aws_ecr_lifecycle_policy" "apply_executor" {
  repository = aws_ecr_repository.apply_executor.name
  policy = jsonencode({
    rules = [
      {
        # COUNT, not age. Every `docker push :prod` moves the tag and orphans
        # the previous image as untagged, and `shared/python/**` is in this
        # image's change filter -- so a busy day pushes dozens. Measured
        # 2026-09-03: 46 images / 46.7 GB, of which exactly ONE was tagged and
        # 45 were same-day orphans that a 7-day rule had not reached yet, on a
        # Lambda with zero invocations that month.
        #
        # Three untagged is a rollback window (current, previous, one spare),
        # not a graveyard.
        rulePriority = 1
        description  = "Keep the last 3 untagged images; expire older orphans"
        selection = {
          tagStatus   = "untagged"
          countType   = "imageCountMoreThan"
          countNumber = 3
        }
        action = { type = "expire" }
      },
      {
        rulePriority = 2
        description  = "Keep last 10 tagged images"
        selection = {
          tagStatus     = "tagged"
          tagPrefixList = ["prod", "sha-"]
          countType     = "imageCountMoreThan"
          countNumber   = 10
        }
        action = { type = "expire" }
      },
    ]
  })
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
  tags = local.apply_executor_tags
}

resource "aws_iam_role_policy_attachment" "apply_executor_basic" {
  role       = aws_iam_role.apply_executor_exec.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy_attachment" "apply_executor_xray" {
  role       = aws_iam_role.apply_executor_exec.name
  policy_arn = "arn:aws:iam::aws:policy/AWSXRayDaemonWriteAccess"
}

resource "aws_iam_role_policy" "apply_executor_inline" {
  name = "apply-executor-access"
  role = aws_iam_role.apply_executor_exec.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "ReadTailoredResume"
        Effect = "Allow"
        Action = ["s3:GetObject"]
        Resource = [
          "${aws_s3_bucket.resumes.arn}/tailoring/*",
          "${aws_s3_bucket.resumes.arn}/resume-assets/*",
          "${aws_s3_bucket.resumes.arn}/guest/*",
          "${aws_s3_bucket.resumes.arn}/*/base/*",
          "${aws_s3_bucket.resumes.arn}/*/generated/*",
        ]
      },
      {
        Sid      = "WriteAuditProof"
        Effect   = "Allow"
        Action   = ["s3:PutObject", "s3:GetObject"]
        Resource = ["${aws_s3_bucket.resumes.arn}/auto-apply-audit/*"]
      },
      {
        # Bucket default SSE uses aws/s3; allow via S3 service only.
        Sid      = "S3SseKms"
        Effect   = "Allow"
        Action   = ["kms:Decrypt", "kms:GenerateDataKey"]
        Resource = ["*"]
        Condition = {
          StringEquals = {
            "kms:ViaService" = "s3.${data.aws_region.current.name}.amazonaws.com"
          }
        }
      },
      {
        Sid    = "SsmReadMongo"
        Effect = "Allow"
        Action = ["ssm:GetParameter", "ssm:GetParameters"]
        Resource = [
          "arn:aws:ssm:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:parameter${var.ssm_path_prefix}/mongodb-uri",
        ]
      },
      {
        Sid       = "SsmSecureStringDecrypt"
        Effect    = "Allow"
        Action    = ["kms:Decrypt"]
        Resource  = ["*"]
        Condition = {
          StringEquals = {
            "kms:ViaService" = "ssm.${data.aws_region.current.name}.amazonaws.com"
          }
        }
      },
      {
        Sid      = "DecryptAtsCredentials"
        Effect   = "Allow"
        Action   = ["kms:Decrypt", "kms:GenerateDataKey", "kms:DescribeKey"]
        Resource = [aws_kms_key.credentials.arn]
      },
    ]
  })
}

resource "aws_cloudwatch_log_group" "apply_executor" {
  name              = "/aws/lambda/${var.project}-apply-executor-${var.environment}"
  retention_in_days = var.apply_executor_log_retention_days
  tags              = local.apply_executor_tags
}

resource "aws_lambda_function" "apply_executor" {
  function_name = "${var.project}-apply-executor-${var.environment}"
  role          = aws_iam_role.apply_executor_exec.arn
  package_type  = "Image"
  image_uri     = "${aws_ecr_repository.apply_executor.repository_url}:${var.apply_executor_image_tag}"

  memory_size = 2048 # Chromium wants ~1.5 GB
  timeout     = 300  # one form is seconds; headroom for slow multi-step forms

  # Cost ceiling + per-domain politeness cap: at most 5 browsers at once.
  reserved_concurrent_executions = 5

  environment {
    variables = {
      ENVIRONMENT              = var.environment
      RESUME_S3_BUCKET         = aws_s3_bucket.resumes.bucket
      DB_NAME                  = var.db_name
      USE_SSM_SECRETS          = "true"
      SSM_MONGODB_URI          = "${var.ssm_path_prefix}/mongodb-uri"
      APPLY_EXECUTOR_DIAGNOSTICS = "true" # selftest + probe_form; submit_test still gated
      CREDENTIALS_KMS_KEY_ID     = aws_kms_key.credentials.arn
    }
  }

  tracing_config { mode = "Active" }

  tags = local.apply_executor_tags

  # CI pushes new images + updates the function code; Terraform must not revert.
  lifecycle {
    ignore_changes = [image_uri]
  }

  depends_on = [
    aws_iam_role_policy_attachment.apply_executor_basic,
    aws_iam_role_policy_attachment.apply_executor_xray,
    aws_cloudwatch_log_group.apply_executor,
  ]
}

resource "aws_cloudwatch_metric_alarm" "apply_executor_errors" {
  alarm_name          = "${var.project}-apply-executor-${var.environment}-errors"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 1
  metric_name         = "Errors"
  namespace           = "AWS/Lambda"
  period              = 300
  statistic           = "Sum"
  threshold           = 3
  treat_missing_data  = "notBreaching"
  alarm_description   = "Apply-executor Lambda errors ≥3 in 5m"
  dimensions = {
    FunctionName = aws_lambda_function.apply_executor.function_name
  }
  tags = local.apply_executor_tags
}

resource "aws_cloudwatch_metric_alarm" "apply_executor_throttles" {
  alarm_name          = "${var.project}-apply-executor-${var.environment}-throttles"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 1
  metric_name         = "Throttles"
  namespace           = "AWS/Lambda"
  period              = 300
  statistic           = "Sum"
  threshold           = 1
  treat_missing_data  = "notBreaching"
  alarm_description   = "Apply-executor throttled (reserved concurrency ceiling)"
  dimensions = {
    FunctionName = aws_lambda_function.apply_executor.function_name
  }
  tags = local.apply_executor_tags
}

resource "aws_cloudwatch_metric_alarm" "apply_executor_duration" {
  alarm_name          = "${var.project}-apply-executor-${var.environment}-duration"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 1
  metric_name         = "Duration"
  namespace           = "AWS/Lambda"
  period              = 300
  statistic           = "Maximum"
  threshold           = 270000 # 270s — warn before 300s hard timeout
  treat_missing_data  = "notBreaching"
  alarm_description   = "Apply-executor duration near 300s timeout"
  dimensions = {
    FunctionName = aws_lambda_function.apply_executor.function_name
  }
  tags = local.apply_executor_tags
}

resource "aws_budgets_budget" "apply_executor" {
  name         = "${var.project}-apply-executor-${var.environment}"
  budget_type  = "COST"
  limit_amount = tostring(var.apply_executor_monthly_budget)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  cost_filter {
    name   = "TagKeyValue"
    values = ["user:Component$apply-executor"]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 50
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = ["harshith.siddardha@gmail.com"]
  }
  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 75
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = ["harshith.siddardha@gmail.com"]
  }
  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 90
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = ["harshith.siddardha@gmail.com"]
  }
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

# The CI deployer also ships the EC2 ingest runner (the crawl), which is NOT a
# Lambda -- see the `deploy-ingest-runner` job in .github/workflows/deploy.yml.
# It stages a tarball in the deploy prefix and runs the installer over SSM.
#
# Scoped deliberately narrowly: PutObject on the `deploy/ingest-runner/` PREFIX
# only, not the bucket. The bucket is `jobpilot-resumes-*` and holds user
# résumés; a deploy identity has no business writing anywhere else in it, and
# `s3:*` on the bucket would have handed CI exactly that.
#
# SendCommand is scoped to the one instance AND the one document. Without the
# document ARN, `ssm:SendCommand` on an instance permits ANY SSM document
# against it, which is arbitrary remote code execution on the crawler box from
# a CI token.
resource "aws_iam_user_policy" "ci_ingest_runner_deploy" {
  name = "jobpilot-ci-ingest-runner-deploy"
  user = "jobpilot-ci-deployer"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "StageIngestRunnerArtifact"
        Effect = "Allow"
        Action = ["s3:PutObject"]
        Resource = [
          "arn:aws:s3:::jobpilot-resumes-prod-032398581420/deploy/ingest-runner/*"
        ]
      },
      {
        Sid    = "RunInstallerOnTheIngestBox"
        Effect = "Allow"
        Action = ["ssm:SendCommand"]
        Resource = [
          "arn:aws:ec2:us-east-1:032398581420:instance/i-06bf0e03e60d9d409",
          "arn:aws:ssm:us-east-1::document/AWS-RunShellScript",
        ]
      },
      {
        # Reading a command's own result carries no instance scope in IAM, so
        # this one cannot be narrowed further. It is read-only.
        Sid      = "ReadCommandResult"
        Effect   = "Allow"
        Action   = ["ssm:GetCommandInvocation", "ssm:ListCommandInvocations"]
        Resource = "*"
      },
    ]
  })
}

# The CI (GitHub Actions) deployer needs ECR auth + push to build/push the
# apply-executor container image (the four zip Lambdas + layer never needed ECR).
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

output "apply_executor_function_name" {
  value = aws_lambda_function.apply_executor.function_name
}

# Single execution role shared by all four functions. Least-privilege-ish:
# logs + X-Ray (managed), then inline SSM read, Bedrock invoke, optional S3,
# SES send, and self-invoke (for the resume async-job branch).

data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "lambda_exec" {
  name               = "${var.project}-backend-exec-${var.environment}"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

# CloudWatch Logs
resource "aws_iam_role_policy_attachment" "basic" {
  role       = aws_iam_role.lambda_exec.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# X-Ray write (tracing_config = Active)
resource "aws_iam_role_policy_attachment" "xray" {
  role       = aws_iam_role.lambda_exec.name
  policy_arn = "arn:aws:iam::aws:policy/AWSXRayDaemonWriteAccess"
}

data "aws_iam_policy_document" "inline" {
  # Read secrets from SSM Parameter Store (USE_SSM_SECRETS=true in prod)
  statement {
    sid     = "SsmRead"
    actions = ["ssm:GetParameter", "ssm:GetParameters", "ssm:GetParametersByPath"]
    resources = [
      "arn:aws:ssm:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:parameter${var.ssm_path_prefix}",
      "arn:aws:ssm:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:parameter${var.ssm_path_prefix}/*",
    ]
  }

  # Decrypt SecureString params
  statement {
    sid       = "KmsDecrypt"
    actions   = ["kms:Decrypt"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["ssm.${data.aws_region.current.name}.amazonaws.com"]
    }
  }

  # Claude on Bedrock
  statement {
    sid       = "BedrockInvoke"
    actions   = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
    resources = ["*"]
  }

  # SES (auth password-reset / notification email)
  statement {
    sid       = "SesSend"
    actions   = ["ses:SendEmail", "ses:SendRawEmail"]
    resources = ["*"]
  }

  # Self-invoke: the resume Lambda dispatches async jobs by invoking itself.
  # Scoped to this project's functions to avoid an IAM<->Lambda dependency cycle.
  statement {
    sid       = "SelfInvoke"
    actions   = ["lambda:InvokeFunction"]
    resources = ["arn:aws:lambda:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:function:${var.project}-*-${var.environment}"]
  }
}

resource "aws_iam_role_policy" "inline" {
  name   = "${var.project}-backend-inline-${var.environment}"
  role   = aws_iam_role.lambda_exec.id
  policy = data.aws_iam_policy_document.inline.json
}

# S3 access for résumé files (bucket created in s3_resumes.tf).
data "aws_iam_policy_document" "s3" {
  statement {
    sid     = "ResumeS3"
    actions = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:ListBucket"]
    resources = [
      aws_s3_bucket.resumes.arn,
      "${aws_s3_bucket.resumes.arn}/*",
    ]
  }
}

resource "aws_iam_role_policy" "s3" {
  name   = "${var.project}-backend-s3-${var.environment}"
  role   = aws_iam_role.lambda_exec.id
  policy = data.aws_iam_policy_document.s3.json
}

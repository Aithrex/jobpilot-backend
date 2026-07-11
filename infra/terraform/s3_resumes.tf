# Private S3 bucket for résumé files (uploads, generated PDFs/DOCX).
# Not public — accessed only by the Lambdas via the execution role.

resource "aws_s3_bucket" "resumes" {
  bucket = "${var.project}-resumes-${var.environment}-${data.aws_caller_identity.current.account_id}"
}

resource "aws_s3_bucket_public_access_block" "resumes" {
  bucket                  = aws_s3_bucket.resumes.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "resumes" {
  bucket = aws_s3_bucket.resumes.id
  versioning_configuration {
    status = "Enabled"
  }
}

# Keep storage tidy: expire old non-current versions.
resource "aws_s3_bucket_lifecycle_configuration" "resumes" {
  bucket = aws_s3_bucket.resumes.id
  rule {
    id     = "expire-noncurrent"
    status = "Enabled"
    filter {}
    noncurrent_version_expiration {
      noncurrent_days = 90
    }
  }
}

# ============================================================================
# DRAFT — requires terraform plan + human review before apply.
# ============================================================================
# SSE-KMS default encryption (auto-apply Tranche 1 §4 hardening). Uses the
# AWS-managed aws/s3 KMS key (kms_master_key_id omitted) so no extra key policy
# is needed; substitute a dedicated CMK ARN here if résumé objects warrant one.
# bucket_key_enabled = true amortizes KMS calls to cut per-object cost/throttle.
resource "aws_s3_bucket_server_side_encryption_configuration" "resumes" {
  bucket = aws_s3_bucket.resumes.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "aws:kms"
    }
    bucket_key_enabled = true
  }
}

# DRAFT — requires terraform plan + human review before apply.
# TLS-only: deny any request made over plaintext HTTP (aws:SecureTransport
# false). This is a restrictive Deny (not a public grant), so it is compatible
# with block_public_policy = true on the public-access-block above.
data "aws_iam_policy_document" "resumes_tls_only" {
  statement {
    sid       = "DenyInsecureTransport"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [aws_s3_bucket.resumes.arn, "${aws_s3_bucket.resumes.arn}/*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "resumes_tls_only" {
  bucket = aws_s3_bucket.resumes.id
  policy = data.aws_iam_policy_document.resumes_tls_only.json
}

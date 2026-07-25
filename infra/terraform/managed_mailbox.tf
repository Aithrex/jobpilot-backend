# ============================================================================
# DRAFT — human review + `terraform plan` required before apply.
# ============================================================================
# Managed Application Mailbox — inbound quarantine store (managed-mailbox-v1).
#
# What this file adds (all NEW resources — nothing existing is modified here):
#   * A dedicated, private, versioned, SSE-KMS S3 bucket for raw MIME +
#     validated attachment bytes  (spec §3 layout: mail/raw, mail/text,
#     mail/html, mail/att).  Metadata NEVER lives here — it lives in Mongo
#     (spec §2/§6), so the lifecycle rule only expires the raw MIME prefix.
#   * A DEDICATED KMS CMK + alias with a least-privilege key policy (separate
#     from the SSM-managed key and the credential-vault CMK, so mail-object
#     encryption can be revoked/audited independently).
#   * A least-privilege IAM policy on the EXISTING resume/shared Lambda role
#     (aws_iam_role.lambda_exec in iam.tf): object-level Get/Put on mail/* only,
#     and GenerateDataKey/Decrypt on THIS CMK only. No bucket-wide, no
#     account-wide grants.
#
# The Cloudflare Email Worker holds NO AWS creds; it only ever receives a
# one-use, single-object, short-TTL presigned PUT minted by the Lambda (spec
# §4).  The Lambda is therefore the sole KMS/S3 principal for this bucket.
# ============================================================================

locals {
  # Global-unique default mirrors the résumé bucket convention (account-id
  # suffix). Set var.mail_bucket_name to pin an exact name (spec §7 example:
  # "jobpilot-mail-prod").
  mail_bucket_name = coalesce(
    var.mail_bucket_name,
    "${var.project}-mail-${var.environment}-${data.aws_caller_identity.current.account_id}",
  )
}

# ----------------------------------------------------------------------------
# KMS CMK for mail objects
# ----------------------------------------------------------------------------
data "aws_iam_policy_document" "mail_kms" {
  # Root retains full control so account admins/IAM keep authorization and we
  # can never lock ourselves out of the key (AWS-recommended baseline).
  statement {
    sid       = "EnableRootAccountAdmin"
    effect    = "Allow"
    actions   = ["kms:*"]
    resources = ["*"]
    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::${data.aws_caller_identity.current.account_id}:root"]
    }
  }

  # The ONLY workload principal: the shared Lambda execution role. Scoped to the
  # exact crypto ops the pipeline needs — GenerateDataKey (S3 SSE-KMS PutObject
  # via the presigned URL) and Decrypt (commit-phase GetObject of the raw MIME).
  statement {
    sid       = "AllowResumeLambdaUse"
    effect    = "Allow"
    actions   = ["kms:GenerateDataKey", "kms:Decrypt", "kms:DescribeKey"]
    resources = ["*"]
    principals {
      type        = "AWS"
      identifiers = [aws_iam_role.lambda_exec.arn]
    }
  }
}

resource "aws_kms_key" "mail" {
  description             = "${var.project} ${var.environment} — managed mailbox S3 objects (raw MIME + attachments)"
  deletion_window_in_days = 30
  enable_key_rotation     = true
  policy                  = data.aws_iam_policy_document.mail_kms.json
}

resource "aws_kms_alias" "mail" {
  name          = "alias/${var.project}-mail-${var.environment}"
  target_key_id = aws_kms_key.mail.key_id
}

# ----------------------------------------------------------------------------
# S3 bucket — private, versioned, SSE-KMS, TLS-only, raw/ retention
# ----------------------------------------------------------------------------
resource "aws_s3_bucket" "mail" {
  bucket = local.mail_bucket_name
}

resource "aws_s3_bucket_public_access_block" "mail" {
  bucket                  = aws_s3_bucket.mail.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "mail" {
  bucket = aws_s3_bucket.mail.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "mail" {
  bucket = aws_s3_bucket.mail.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.mail.arn
    }
    # Amortize KMS calls across objects to cut per-object cost/throttle.
    bucket_key_enabled = true
  }
}

# Raw MIME is a quarantine copy — expire it after the configured retention.
# Metadata (threads/messages/attachments) lives in Mongo and is durable, so it
# is deliberately NOT covered here. Only the mail/raw/ prefix expires.
resource "aws_s3_bucket_lifecycle_configuration" "mail" {
  bucket = aws_s3_bucket.mail.id

  rule {
    id     = "expire-raw-mime"
    status = "Enabled"
    filter {
      prefix = "mail/raw/"
    }
    expiration {
      days = var.mail_raw_retention_days
    }
    noncurrent_version_expiration {
      noncurrent_days = 30
    }
  }

  # Clean up incomplete multipart uploads (large attachment PUTs) bucket-wide.
  rule {
    id     = "abort-incomplete-mpu"
    status = "Enabled"
    filter {}
    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

# TLS-only: deny any plaintext-HTTP request. Restrictive Deny (not a public
# grant) — compatible with block_public_policy = true above. Mirrors the
# résumé bucket policy.
data "aws_iam_policy_document" "mail_tls_only" {
  statement {
    sid       = "DenyInsecureTransport"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [aws_s3_bucket.mail.arn, "${aws_s3_bucket.mail.arn}/*"]
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

resource "aws_s3_bucket_policy" "mail_tls_only" {
  bucket = aws_s3_bucket.mail.id
  policy = data.aws_iam_policy_document.mail_tls_only.json
}

# ----------------------------------------------------------------------------
# IAM — least-privilege grant on the EXISTING shared Lambda role (iam.tf)
# ----------------------------------------------------------------------------
# Object-level only, mail/* prefix only, and crypto only on THIS CMK. No
# ListBucket, no bucket-wide s3 actions, no account-wide KMS.
data "aws_iam_policy_document" "mail" {
  statement {
    sid       = "ManagedMailboxS3Objects"
    actions   = ["s3:GetObject", "s3:PutObject"]
    resources = ["${aws_s3_bucket.mail.arn}/mail/*"]
  }

  statement {
    sid       = "ManagedMailboxKms"
    actions   = ["kms:GenerateDataKey", "kms:Decrypt"]
    resources = [aws_kms_key.mail.arn]
  }
}

resource "aws_iam_role_policy" "mail" {
  name   = "${var.project}-backend-mail-${var.environment}"
  role   = aws_iam_role.lambda_exec.id
  policy = data.aws_iam_policy_document.mail.json
}

# ----------------------------------------------------------------------------
# Outputs (kept inline, matching kms_credentials.tf's self-contained style)
# ----------------------------------------------------------------------------
output "mail_bucket_name" {
  value       = aws_s3_bucket.mail.bucket
  description = "SSE-KMS bucket for managed-mailbox raw MIME + attachments (wire to APPLY_MAIL_BUCKET)."
}

output "mail_kms_key_arn" {
  value       = aws_kms_key.mail.arn
  description = "CMK ARN encrypting the mail bucket (wire to APPLY_MAIL_KMS_KEY_ID)."
}

output "mail_kms_key_id" {
  value       = aws_kms_key.mail.key_id
  description = "CMK key id for the mail bucket."
}

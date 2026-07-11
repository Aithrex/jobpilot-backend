# ============================================================================
# DRAFT — human review + `terraform plan` required before apply.
# ============================================================================
# Dedicated CMK for the ATS credential vault (auto-apply Tranche 1 §4).
#
# Why a DEDICATED key (not the SSM-managed one):
#   * kms_cipher.py envelope-encrypts user ATS passwords under this CMK
#     (GenerateDataKey / Decrypt with a per-record encryption context), so a
#     key-policy or IAM change can revoke ALL credential decryption without
#     touching SSM SecureString reads.
#   * Every Decrypt (i.e., every password reveal) is a distinct CloudTrail
#     event against this key ARN — an auditable, alertable choke point.
#
# Deliberate choices:
#   * enable_key_rotation: annual automatic rotation of the CMK backing key.
#     Old data keys stay decryptable (KMS retains prior backing keys) — no
#     re-encryption migration needed.
#   * deletion_window 30 days: maximum runway to catch an accidental
#     `terraform destroy` before user credentials become unrecoverable.
#   * No aws_kms_key policy block: the default key policy (root account has
#     full control, IAM policies gate usage) keeps authorization in iam.tf —
#     see the AtsCredentialVault statement to add there (scoped to this ARN).
#
# Wiring after apply (reported edits, not in this file):
#   * iam.tf: allow kms:GenerateDataKey + kms:Decrypt on this key ARN only.
#   * lambda.tf env: CREDENTIALS_KMS_KEY_ID = aws_kms_key.credentials.arn
#     (a key IDENTIFIER, not a secret — a plain env var is correct here).
# ============================================================================

resource "aws_kms_key" "credentials" {
  description             = "${var.project} ${var.environment} — ATS credential vault (envelope encryption for ats_credentials.password_blob)"
  deletion_window_in_days = 30
  enable_key_rotation     = true
}

resource "aws_kms_alias" "credentials" {
  name          = "alias/${var.project}-credentials-${var.environment}"
  target_key_id = aws_kms_key.credentials.key_id
}

output "credentials_kms_key_arn" {
  value       = aws_kms_key.credentials.arn
  description = "CMK ARN for the ATS credential vault (wire to CREDENTIALS_KMS_KEY_ID)."
}

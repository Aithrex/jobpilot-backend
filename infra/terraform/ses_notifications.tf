# SES bounce/complaint feedback → SNS → the auth Lambda's public webhook
# (POST /api/auth/ses-notifications, which rides the existing /api/auth/{proxy+}
# edge route, so no new API Gateway route is needed). The handler verifies the
# SNS signature and records suppressions; SES's account-level suppression list
# (already enabled for BOUNCE + COMPLAINT) is the belt to this suspenders.

resource "aws_sns_topic" "ses_notifications" {
  name = "${var.project}-ses-notifications-${var.environment}"
}

# Let SES (this account only) publish bounce/complaint events to the topic.
data "aws_iam_policy_document" "ses_sns_publish" {
  statement {
    sid     = "AllowSESPublish"
    effect  = "Allow"
    actions = ["SNS:Publish"]
    principals {
      type        = "Service"
      identifiers = ["ses.amazonaws.com"]
    }
    resources = [aws_sns_topic.ses_notifications.arn]
    condition {
      test     = "StringEquals"
      variable = "AWS:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }
}

resource "aws_sns_topic_policy" "ses_notifications" {
  arn    = aws_sns_topic.ses_notifications.arn
  policy = data.aws_iam_policy_document.ses_sns_publish.json
}

# Route the aspirely.me identity's Bounce + Complaint feedback to the topic.
# (Identity was created out-of-band via the SES v2 CLI; referenced by name.)
resource "aws_ses_identity_notification_topic" "bounce" {
  topic_arn                = aws_sns_topic.ses_notifications.arn
  notification_type        = "Bounce"
  identity                 = "aspirely.me"
  include_original_headers = true
}

resource "aws_ses_identity_notification_topic" "complaint" {
  topic_arn                = aws_sns_topic.ses_notifications.arn
  notification_type        = "Complaint"
  identity                 = "aspirely.me"
  include_original_headers = true
}

# HTTPS delivery to the webhook. Our handler auto-confirms the subscription by
# fetching the SubscribeURL, so endpoint_auto_confirms lets `apply` complete.
# NOTE: the handler code must be DEPLOYED before this is applied.
resource "aws_sns_topic_subscription" "ses_https" {
  topic_arn              = aws_sns_topic.ses_notifications.arn
  protocol               = "https"
  endpoint               = "${var.frontend_url}/api/auth/ses-notifications"
  endpoint_auto_confirms = true
}

# The four service functions, one per services/<svc>/ package.
# Each bundles only its own dir (build/<svc>.zip) and shares the layer.

resource "aws_lambda_function" "service" {
  for_each = var.service_config

  function_name = "${var.project}-${each.key}-${var.environment}"
  role          = aws_iam_role.lambda_exec.arn
  runtime       = var.runtime
  handler       = "lambda_handler.handler"

  filename         = "${var.artifacts_dir}/${each.key}.zip"
  source_code_hash = filebase64sha256("${var.artifacts_dir}/${each.key}.zip")

  layers      = [aws_lambda_layer_version.shared.arn]
  memory_size = each.value.memory
  timeout     = each.value.timeout

  tracing_config {
    mode = "Active"
  }

  environment {
    variables = {
      ENVIRONMENT     = var.environment
      USE_SSM_SECRETS = "true"
      ALLOWED_ORIGINS = var.allowed_origins
      BEDROCK_REGION  = var.bedrock_region
      LOG_LEVEL       = var.log_level
      # AWS_REGION is injected by the Lambda runtime (reserved — do not set).

      # Non-secret config
      DB_NAME            = var.db_name
      RESUME_S3_BUCKET   = aws_s3_bucket.resumes.bucket
      FRONTEND_URL       = var.frontend_url
      BACKEND_PUBLIC_URL = var.frontend_url
      # Transactional + notification email. EMAIL_PROVIDER selects the transport
      # (zeptomail | ses). ZeptoMail was adopted while SES production access was
      # still pending; that access has since been GRANTED and aspirely.me is a
      # verified, DKIM-signed SES identity, so both transports are live.
      #
      # EMAIL_FALLBACK_PROVIDER is the safety net. email_service swallows send
      # errors by design (a dead mailer must never break signup or password
      # reset), which means a bad credential silently drops 100% of mail — an
      # invalid ZeptoMail token did exactly that to password-reset emails. With
      # a fallback set, the other transport (independent credentials) picks up
      # the send and logs `[email:failover]`. Set to "none" to disable.
      # From/Reply-To are shared across providers (email_service falls back to
      # the SES_* names, so these stay authoritative).
      EMAIL_PROVIDER          = "zeptomail"
      EMAIL_FALLBACK_PROVIDER = "ses"
      SES_FROM_ADDRESS        = "Aspirely <no-reply@aspirely.me>"
      SES_REPLY_TO            = "support@aithrex.tech"
      # ZeptoMail Send Mail token (SecureString via SSM). Non-US Mail Agents also
      # need ZEPTOMAIL_API_URL (default is the .com host).
      SSM_ZEPTOMAIL_TOKEN = "${var.ssm_path_prefix}/zeptomail-token"
      # Expected SNS topic for SES bounce/complaint feedback — the webhook
      # (POST /api/auth/ses-notifications) rejects messages from any other topic.
      SES_SNS_TOPIC_ARN = aws_sns_topic.ses_notifications.arn

      # LLM provider selection + Gemini credentials. LLM_PROVIDER is read from
      # SSM (not set as a plain env var here) so switching Claude <-> Gemini is
      # a single `aws ssm put-parameter .../llm-provider` call with NO redeploy
      # and no terraform apply. Absent/unset -> claude (today's behavior).
      # The /llm-provider parameter is a plain String; only the API key is a
      # SecureString. See services/llm_providers/__init__.py.
      SSM_LLM_PROVIDER   = "${var.ssm_path_prefix}/llm-provider"
      SSM_GEMINI_API_KEY = "${var.ssm_path_prefix}/gemini-api-key"

      # SSM parameter PATHS (utils/ssm_config reads these env vars, then fetches
      # the SecureString values at those paths). Seed them with scripts/seed_ssm.sh.
      SSM_MONGODB_URI              = "${var.ssm_path_prefix}/mongodb-uri"
      SSM_JWT_SECRET               = "${var.ssm_path_prefix}/jwt-secret-key"
      SSM_IPINFO_TOKEN             = "${var.ssm_path_prefix}/ipinfo-token"
      SSM_JSEARCH_API_KEY          = "${var.ssm_path_prefix}/jsearch-api-key"
      SSM_APIFY_API_KEY            = "${var.ssm_path_prefix}/apify-api-key"
      SSM_JOB_SEARCH_PASSWORD_HASH = "${var.ssm_path_prefix}/job-search-password-hash"
      SSM_GITHUB_PAT               = "${var.ssm_path_prefix}/github-pat"
      # Google sign-in (social login) OAuth 2.0 Web client — separate from the
      # Gmail-readonly GOOGLE_OAUTH_* pair. Gates provider_configured("google").
      SSM_GOOGLE_SIGNIN_CLIENT_ID     = "${var.ssm_path_prefix}/google-signin-client-id"
      SSM_GOOGLE_SIGNIN_CLIENT_SECRET = "${var.ssm_path_prefix}/google-signin-client-secret"
      # "Connect GitHub / LinkedIn" data-integration OAuth (resume Lambda,
      # /api/resume/connect/*). Separate creds from social sign-in; tokens are
      # Fernet-encrypted at rest with INTEGRATION_TOKEN_ENCRYPTION_KEY.
      SSM_GITHUB_CONNECT_CLIENT_ID         = "${var.ssm_path_prefix}/github-connect-client-id"
      SSM_GITHUB_CONNECT_CLIENT_SECRET     = "${var.ssm_path_prefix}/github-connect-client-secret"
      SSM_LINKEDIN_CONNECT_CLIENT_ID       = "${var.ssm_path_prefix}/linkedin-connect-client-id"
      SSM_LINKEDIN_CONNECT_CLIENT_SECRET   = "${var.ssm_path_prefix}/linkedin-connect-client-secret"
      SSM_INTEGRATION_TOKEN_ENCRYPTION_KEY = "${var.ssm_path_prefix}/integration-token-encryption-key"
      # Gmail read-only (Track B, /api/resume/gmail/*). Restricted scope — stays
      # dark in prod behind GMAIL_OAUTH_ENABLED (unset = OFF) until Google
      # verification + CASA Tier 2 clears. Redirect URI is non-secret (plain env).
      GOOGLE_OAUTH_REDIRECT_URI      = "${var.frontend_url}/oauth/gmail/callback"
      GMAIL_OAUTH_ENABLED            = "true" # ON in prod. App is in Google "Testing", so only added test users can complete the gmail.readonly flow; stays test-user-gated until OAuth verification + CASA Tier 2 pass.
      SSM_GOOGLE_OAUTH_CLIENT_ID     = "${var.ssm_path_prefix}/google-oauth-client-id"
      SSM_GOOGLE_OAUTH_CLIENT_SECRET = "${var.ssm_path_prefix}/google-oauth-client-secret"
      SSM_GMAIL_TOKEN_ENCRYPTION_KEY = "${var.ssm_path_prefix}/gmail-token-encryption-key"

      # DRAFT — requires terraform plan + human review before apply.
      # Auto-apply Tranche 1. CREDENTIALS_KMS_KEY_ID is a plain identifier (NOT a
      # secret) — the CMK the credential vault (kms_cipher.py) envelope-encrypts
      # under. APPROVAL_LINK_SECRET is the dedicated HMAC secret for signed
      # approve-links (§2), read as a SecureString via SSM_APPROVAL_LINK_SECRET.
      CREDENTIALS_KMS_KEY_ID   = aws_kms_key.credentials.arn
      SSM_APPROVAL_LINK_SECRET = "${var.ssm_path_prefix}/approval-link-secret"

      # Managed Application Mailbox (managed-mailbox-v1 §7). This `environment`
      # block is inside `for_each = var.service_config`, so EVERY service Lambda
      # (auth · jobs · resume · billing) already receives these vars — auth NEEDS
      # APPLY_MAIL_DOMAIN because `auth.register` / `oauth_service.upsert_user`
      # now auto-provision the mailbox at signup (AUTOAPPLY_REPAIR_SPEC §6);
      # without the domain `provision()` would mint a bare local part with no
      # host. Do NOT move these into a per-service override. The feature stays
      # INERT until APPLY_MAIL_DOMAIN is set AND APPLY_MAILBOX_ENABLED flips true
      # (spec §11/§12). The bucket + CMK below are provisioned in
      # managed_mailbox.tf, so we wire them directly.
      APPLY_MAIL_DOMAIN     = "my-private-mail.com"
      APPLY_MAILBOX_ENABLED = "true"
      # Non-secret store identifiers (key id is NOT a secret — same rationale as
      # CREDENTIALS_KMS_KEY_ID above; a plain env var is correct, no SSM needed).
      APPLY_MAIL_BUCKET     = aws_s3_bucket.mail.bucket
      APPLY_MAIL_KMS_KEY_ID = aws_kms_key.mail.arn
      # Outbound relay sender for forwarding a recruiter email to the user's real
      # inbox — JobPilot-owned, verified in ZeptoMail (aspirely.me domain), NEVER
      # the recruiter. Empty => forwarding stays inert.
      MAIL_FORWARD_FROM = "notifications@aspirely.me"
      # Reply-relay sending mode (privacy-preserving reply back to the recruiter).
      # Held OFF until my-private-mail.com is verified as a ZeptoMail sending
      # domain (SPF/DKIM in Cloudflare — see docs/ops/managed-mailbox-setup.md
      # "Reply-relay sending domain"). OFF => relay From = MAIL_FORWARD_FROM with a
      # "(via My Private Mail)" display; ON => relay From = the user's PRIMARY
      # managed alias. NEVER the user's real email in either mode.
      APPLY_MAIL_SEND_AS_ALIAS    = "false"
      APPLY_MAIL_SENDING_VERIFIED = "false"
      # Shared HMAC secret with the Cloudflare Worker's secret binding — the ONLY
      # secret this feature adds. SecureString at this SSM path; seed the SAME
      # value into the Worker via `wrangler secret put MAIL_INBOUND_HMAC_SECRET`
      # (spec §7/§12). Unset ⇒ slot/commit HMAC verify fails closed (403).
      SSM_MAIL_INBOUND_HMAC_SECRET = "${var.ssm_path_prefix}/mail-inbound-hmac-secret"

      # Guest onboarding (no-signup /start flow). ON in prod. Server-side salt
      # for guest ip_hash via SSM (unset ⇒ hash_ip fails closed, feature still
      # works). Gate = env flag AND the admin `enabled` knob (default true).
      GUEST_ONBOARDING_ENABLED = "true"
      SSM_GUEST_IP_HASH_SALT   = "${var.ssm_path_prefix}/guest-ip-hash-salt"
      # Guest v2. Both fail closed when unset: without the signal key, identity
      # signals are neither stored nor matched (quota subjects stay standalone);
      # without the download secret, guest download tokens cannot be minted
      # (503, never 401). Rotating the signal key orphans existing HMAC links.
      SSM_GUEST_SIGNAL_HMAC_KEY       = "${var.ssm_path_prefix}/guest-signal-hmac-key"
      SSM_GUEST_DOWNLOAD_TOKEN_SECRET = "${var.ssm_path_prefix}/guest-download-token-secret"

      # Voice Graph (Compass addendum §6.12). ON in prod. Writing samples are
      # Fernet-encrypted at rest via INTEGRATION_TOKEN_ENCRYPTION_KEY (falls back
      # to GMAIL_TOKEN_ENCRYPTION_KEY) — both already wired above, so sample-save
      # works. Unset would default OFF in prod (all routes 404); set explicitly.
      VOICE_GRAPH_ENABLED = "true"

      # Auto-apply AWS browser fleet (Playwright container Lambda).
      # Stage-4 armed for easy ATS (Greenhouse / Lever / Ashby) + Workday
      # best-effort. LinkedIn/generic stay disabled. Kill via AUTO_SUBMIT_ENABLED
      # or EXECUTOR_REMOTE_ENABLED without a deploy.
      AUTO_APPLY_EXECUTOR           = "remote"
      EXECUTOR_REMOTE_ENABLED       = "true"
      APPLY_EXECUTOR_FUNCTION       = "${var.project}-apply-executor-${var.environment}"
      AUTO_SUBMIT_ENABLED           = "true"
      AUTO_APPLY_ADAPTERS_DISABLED  = "linkedin,generic"
      AUTO_APPLY_PREPARE            = "true"
      AUTO_APPLY_MODES              = "true"
      AUTO_APPLY_RESERVE            = "true"
      AUTO_APPLY_APPROVAL_FOLD      = "true"
    }
  }

  depends_on = [
    aws_iam_role_policy_attachment.basic,
    aws_lambda_layer_version.shared,
  ]

  # CI/CD owns CODE; Terraform owns INFRA. The GitHub Actions pipeline updates
  # function code (aws lambda update-function-code) and the layer version
  # (publish-layer-version + update-function-configuration) on every push to
  # main, so Terraform must NOT revert those on the next apply. The initial
  # `terraform apply` still seeds working code from build/<svc>.zip + the layer.
  lifecycle {
    ignore_changes = [source_code_hash, layers]
  }
}

# Let API Gateway invoke each function.
resource "aws_lambda_permission" "apigw" {
  for_each = var.service_config

  statement_id  = "AllowAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.service[each.key].function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.http.execution_arn}/*/*"
}

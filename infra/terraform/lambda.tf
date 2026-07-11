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
      # Transactional + notification email sender (SES). Friendly-from; the
      # aspirely.me domain must be a verified SES identity for sends to succeed.
      SES_FROM_ADDRESS = "Aspirely <no-reply@aspirely.me>"
      SES_REPLY_TO     = "support@aithrex.tech"

      # SSM parameter PATHS (utils/ssm_config reads these env vars, then fetches
      # the SecureString values at those paths). Seed them with scripts/seed_ssm.sh.
      SSM_MONGODB_URI              = "${var.ssm_path_prefix}/mongodb-uri"
      SSM_JWT_SECRET               = "${var.ssm_path_prefix}/jwt-secret-key"
      SSM_IPINFO_TOKEN             = "${var.ssm_path_prefix}/ipinfo-token"
      SSM_GEMINI_API_KEY           = "${var.ssm_path_prefix}/gemini-api-key"
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

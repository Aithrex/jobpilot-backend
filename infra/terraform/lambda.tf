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
      SSM_PATH_PREFIX = var.ssm_path_prefix
      ALLOWED_ORIGINS = var.allowed_origins
      BEDROCK_REGION  = var.bedrock_region
      LOG_LEVEL       = var.log_level
      # AWS_REGION is injected by the Lambda runtime (reserved — do not set).
    }
  }

  depends_on = [
    aws_iam_role_policy_attachment.basic,
    aws_lambda_layer_version.shared,
  ]
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

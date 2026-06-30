# HTTP API (API Gateway v2) fanning each /api/<prefix> to the right Lambda.
# This API is the intended CloudFront origin for /api/* (see infra/README.md).

locals {
  # For every service prefix, register both the bare path and a {proxy+} catch-all
  # so e.g. /api/auth AND /api/auth/login both reach the auth function.
  route_pairs = flatten([
    for svc, cfg in var.service_config : [
      for r in cfg.routes : [
        { rk = "ANY ${r}", svc = svc },
        { rk = "ANY ${r}/{proxy+}", svc = svc },
      ]
    ]
  ])
  routes = { for p in local.route_pairs : p.rk => p.svc }
}

resource "aws_apigatewayv2_api" "http" {
  name          = "${var.project}-api-${var.environment}"
  protocol_type = "HTTP"

  # The Flask apps already set CORS headers; keep gateway CORS off to avoid
  # double headers. Flip this on if you ever bypass the app for OPTIONS.
}

resource "aws_apigatewayv2_integration" "service" {
  for_each = var.service_config

  api_id                 = aws_apigatewayv2_api.http.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.service[each.key].invoke_arn
  integration_method     = "POST"
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "service" {
  for_each = local.routes

  api_id    = aws_apigatewayv2_api.http.id
  route_key = each.key
  target    = "integrations/${aws_apigatewayv2_integration.service[each.value].id}"
}

resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.http.id
  name        = "$default"
  auto_deploy = true

  default_route_settings {
    throttling_burst_limit = 200
    throttling_rate_limit  = 100
  }

  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.apigw.arn
    format = jsonencode({
      requestId      = "$context.requestId"
      ip             = "$context.identity.sourceIp"
      routeKey       = "$context.routeKey"
      status         = "$context.status"
      integrationErr = "$context.integrationErrorMessage"
      responseLength = "$context.responseLength"
    })
  }
}

resource "aws_cloudwatch_log_group" "apigw" {
  name              = "/aws/apigw/${var.project}-${var.environment}"
  retention_in_days = 14
}

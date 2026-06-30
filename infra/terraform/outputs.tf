output "api_endpoint" {
  description = "Invoke URL of the HTTP API (use as the CloudFront /api/* origin)."
  value       = aws_apigatewayv2_api.http.api_endpoint
}

output "api_id" {
  value = aws_apigatewayv2_api.http.id
}

output "function_names" {
  description = "Deployed Lambda function names by service."
  value       = { for k, f in aws_lambda_function.service : k => f.function_name }
}

output "function_arns" {
  value = { for k, f in aws_lambda_function.service : k => f.arn }
}

output "layer_arn" {
  value = aws_lambda_layer_version.shared.arn
}

output "routes" {
  description = "All registered API Gateway route keys -> service."
  value       = local.routes
}

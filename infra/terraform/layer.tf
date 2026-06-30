# Shared Lambda layer — built by scripts/build_layer.sh (build/layer.zip).
# Holds shared code (utils/models/services/schemas) + all Python deps,
# mounted at /opt/python on every function.

locals {
  layer_zip = "${var.artifacts_dir}/layer.zip"
}

resource "aws_lambda_layer_version" "shared" {
  layer_name          = "${var.project}-shared-${var.environment}"
  filename            = local.layer_zip
  source_code_hash    = filebase64sha256(local.layer_zip)
  compatible_runtimes = [var.runtime]
  description         = "Shared code + deps for all JobPilot backend services"
}

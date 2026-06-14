"""
AWS Lambda Handler for Jobs-Resume Microservice

Industry-standard cold start pattern:
  - All initialization at module level (Lambda INIT phase)
  - Handler is thin — just forwards to WSGI + sets metadata
  - Init duration measured once, matches CloudWatch's "Init Duration"

This is the ONLY service that includes the async_job dispatch branch,
used for LLM-powered resume processing and Apify job search aggregation.

Architecture:
    API Gateway (HTTP API) -> Lambda -> apig-wsgi -> Flask App -> MongoDB Atlas
"""

import logging
import os
import time as _time
import builtins

# ── INIT PHASE (runs once per Lambda container) ──────────────────────
# AWS best practice: initialize SDK clients, frameworks, and connections
# outside the handler so Lambda's INIT phase captures the real cost.
# Ref: https://docs.aws.amazon.com/lambda/latest/dg/best-practices.html

_init_start = _time.perf_counter()

# Configure logging
logging.basicConfig(
    level=getattr(logging, os.getenv('LOG_LEVEL', 'INFO')),
    format='%(asctime)s:%(levelname)s:%(name)s:%(message)s'
)
logger = logging.getLogger(__name__)

# Initialize Flask app + WSGI handler at module level (INIT phase)
from app import create_app
from apig_wsgi import make_lambda_handler

_app = create_app()
_wsgi_handler = make_lambda_handler(_app, binary_support=True)

_init_end = _time.perf_counter()
_init_duration_ms = round((_init_end - _init_start) * 1000, 2)

logger.info(f"INIT phase complete: {_init_duration_ms}ms "
            f"(Flask + blueprints + X-Ray SDK)")

# Cold start flag — True only for the first invocation
_cold_start = True

# ── END INIT PHASE ───────────────────────────────────────────────────


def handler(event, context):
    """
    AWS Lambda handler — thin wrapper that forwards to Flask WSGI.

    All heavy initialization already happened at module level (INIT phase).
    This handler just sets metadata and delegates to the WSGI handler.

    Includes the async_job dispatch branch for resume processing and job search.
    """
    try:
        global _cold_start
        is_cold = _cold_start
        _cold_start = False

        # Inject metadata for observability
        builtins._lambda_meta = {
            'cold_start': is_cold,
            'init_duration_ms': _init_duration_ms if is_cold else 0,
            'region': os.environ.get('AWS_REGION', 'local'),
            'memory_mb': int(os.environ.get('AWS_LAMBDA_MEMORY_SIZE', '0')),
            'function_name': os.environ.get('AWS_LAMBDA_FUNCTION_NAME', 'local-dev'),
            'request_id': (
                getattr(context, 'aws_request_id', 'local') if context else 'local'
            ),
        }

        # ── Async job invocation (from Lambda invoking itself) ──
        if event.get("async_job"):
            from services.resume_service import process_async_job
            process_async_job(event)
            return {"statusCode": 200, "body": "OK"}

        # ── Normal API Gateway request ──
        if os.getenv('ENVIRONMENT') != 'prod':
            logger.info(
                f"Incoming request: "
                f"{event.get('requestContext', {}).get('http', {}).get('method')} "
                f"{event.get('rawPath')}"
            )

        return _wsgi_handler(event, context)

    except Exception as e:
        logger.error(f"Lambda handler error: {str(e)}", exc_info=True)
        return {
            'statusCode': 500,
            'headers': {
                'Content-Type': 'application/json',
                'Access-Control-Allow-Origin': os.getenv(
                    'ALLOWED_ORIGINS',
                    'https://manneharshithsiddardha.com'
                ).split(',')[0]
            },
            'body': '{"error": "Internal server error"}'
        }


# For local testing with SAM CLI or direct invocation
if __name__ == "__main__":
    test_event = {
        "version": "2.0",
        "requestContext": {
            "http": {
                "method": "GET",
                "path": "/api/health"
            }
        },
        "rawPath": "/api/health",
        "isBase64Encoded": False
    }

    result = handler(test_event, None)
    print(f"Test result: {result}")

"""AWS Lambda handler for the RESUME microservice.

Cold-start pattern: all init at module level (INIT phase); handler is thin.
This is the ONLY service with the async_job dispatch branch — long LLM résumé
work is run by self-invoking this Lambda with {"async_job": ...} and processed
by services.resume_service.process_async_job.

    API Gateway (HTTP API) -> Lambda -> apig-wsgi -> Flask -> MongoDB Atlas
"""
import logging
import os
import time as _time
import builtins

_init_start = _time.perf_counter()

logging.basicConfig(
    level=getattr(logging, os.getenv('LOG_LEVEL', 'INFO')),
    format='%(asctime)s:%(levelname)s:%(name)s:%(message)s',
)
logger = logging.getLogger(__name__)

from app import create_app
from apig_wsgi import make_lambda_handler

_app = create_app()
_wsgi_handler = make_lambda_handler(_app, binary_support=True)

_init_end = _time.perf_counter()
_init_duration_ms = round((_init_end - _init_start) * 1000, 2)
logger.info(f"INIT phase complete: {_init_duration_ms}ms (resume-service)")

_cold_start = True


def handler(event, context):
    try:
        global _cold_start
        is_cold = _cold_start
        _cold_start = False

        builtins._lambda_meta = {
            'cold_start': is_cold,
            'init_duration_ms': _init_duration_ms if is_cold else 0,
            'region': os.environ.get('AWS_REGION', 'local'),
            'memory_mb': int(os.environ.get('AWS_LAMBDA_MEMORY_SIZE', '0')),
            'function_name': os.environ.get('AWS_LAMBDA_FUNCTION_NAME', 'local-dev'),
            'request_id': getattr(context, 'aws_request_id', 'local') if context else 'local',
        }

        # ── Scheduled job (EventBridge -> this Lambda) ──
        if event.get("scheduled_job"):
            from services.scheduled_jobs import dispatch_scheduled
            result = dispatch_scheduled(event["scheduled_job"])
            logger.info("scheduled_job %s -> %s", event.get("scheduled_job"), result)
            return {"statusCode": 200, "body": "OK"}

        # ── Async job invocation (this Lambda invoking itself) ──
        if event.get("async_job"):
            from services.resume_service import process_async_job
            process_async_job(event)
            return {"statusCode": 200, "body": "OK"}

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
                'Access-Control-Allow-Origin': os.getenv('ALLOWED_ORIGINS', '*').split(',')[0],
            },
            'body': '{"error": "Internal server error"}',
        }


if __name__ == "__main__":
    test_event = {
        "version": "2.0",
        "requestContext": {"http": {"method": "GET", "path": "/api/health"}},
        "rawPath": "/api/health",
        "isBase64Encoded": False,
    }
    print(f"Test result: {handler(test_event, None)}")

"""Application factory for the JOBS microservice (Lambda).

Owns job search/aggregation and the Chrome-extension autofill backend.
Edge routes (external Terraform): ANY /api/jobs/{proxy+}, ANY /api/apply/{proxy+}.

Shared code (utils.*, models.*, schemas.*, services.*) comes from the
`shared/python` Lambda layer; `from services.X` resolves across this Lambda's
local services/ AND the shared layer via PEP 420 namespace packages.
"""
from flask import Flask, request
from flask_cors import CORS
from flask_jwt_extended import JWTManager
from utils.config import AppConfig
import logging
import os

try:
    from pathlib import Path
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parents[2] / '.env')
except ImportError:
    pass

handlers = [logging.StreamHandler()]
if os.getenv('USE_SSM_SECRETS', 'false').lower() != 'true':
    handlers.append(logging.FileHandler('app.log'))
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s:%(levelname)s:%(name)s:%(message)s',
    handlers=handlers,
)
logger = logging.getLogger(__name__)


def create_app():
    app = Flask(__name__)

    app.config['JWT_SECRET_KEY'] = AppConfig.JWT_SECRET_KEY
    app.config['JWT_ACCESS_TOKEN_EXPIRES'] = AppConfig.JWT_ACCESS_TOKEN_EXPIRES

    allowed_origins = os.getenv(
        'ALLOWED_ORIGINS',
        'http://localhost:5173,http://localhost:5174,http://localhost:3000',
    ).split(',')
    CORS(app, resources={
        r"/api/*": {
            "origins": allowed_origins,
            "methods": ["GET", "POST", "PUT", "DELETE", "OPTIONS"],
            "allow_headers": ["Content-Type", "Authorization"],
            "expose_headers": ["Content-Disposition", "X-Trace-Id", "X-Amzn-Trace-Id", "Server-Timing"],
            "supports_credentials": True,
            "max_age": 3600,
        }
    })
    JWTManager(app)

    if os.getenv('USE_SSM_SECRETS', 'false').lower() == 'true':
        try:
            from aws_xray_sdk.core import xray_recorder, patch_all
            from aws_xray_sdk.ext.flask.middleware import XRayMiddleware
            xray_recorder.configure(service='jobs-service')
            XRayMiddleware(app, xray_recorder)
            patch_all()
            logger.info("X-Ray SDK initialized for jobs-service")
        except ImportError:
            logger.warning("aws-xray-sdk not installed, skipping X-Ray instrumentation")
        except Exception as e:
            logger.warning(f"X-Ray initialization failed: {e}")

    @app.after_request
    def set_security_headers(response):
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['X-XSS-Protection'] = '0'
        response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
        if request.path.startswith('/api/'):
            response.headers['Cache-Control'] = 'no-store'
        return response

    from blueprints.jobs import jobs_bp
    app.register_blueprint(jobs_bp, url_prefix='/api/jobs')

    # Chrome-extension autofill backend. REQUIRES "ANY /api/apply/{proxy+}".
    from blueprints.apply import apply_bp
    app.register_blueprint(apply_bp, url_prefix='/api/apply')

    @app.route('/api/health')
    def health():
        return {'status': 'healthy', 'service': 'jobs-service', 'version': '2.0.0'}, 200

    logger.info("Jobs service initialized (jobs, apply blueprints registered)")
    return app


if __name__ == '__main__':
    app = create_app()
    app.run(debug=os.getenv('FLASK_DEBUG', 'false').lower() == 'true', host='0.0.0.0', port=5000)

"""Application factory for the LEARNING microservice (Lambda) — ADR-003.

Owns the Learning / Growth loop endpoints (differentiators → plan → github-sync →
verify → completion post). Edge route (external Terraform): ANY /api/learning/{proxy+}.

This directory is INERT until a deploy filter + Terraform route are added for it
(HUMAN GATE — see the handoff report): no `deploy.yml` filter matches
`services/learning/**` today, so committing it changes nothing in prod. Until
extracted, the same routes are served by the resume Lambda under
`/api/resume/learning/*`; this skeleton makes the domain one-route extractable.

Shared code (utils.*, models.*, schemas.*, services.*) comes from the
`shared/python` Lambda layer via PEP 420 namespace packages.
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
            xray_recorder.configure(service='learning-service')
            XRayMiddleware(app, xray_recorder)
            patch_all()
            logger.info("X-Ray SDK initialized for learning-service")
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

    from blueprints.learning import learning_bp
    app.register_blueprint(learning_bp, url_prefix='/api/learning')

    @app.route('/api/health')
    def health():
        return {'status': 'healthy', 'service': 'learning-service', 'version': '1.0.0'}, 200

    logger.info("Learning service initialized (learning blueprint registered)")
    return app


if __name__ == '__main__':
    app = create_app()
    app.run(debug=os.getenv('FLASK_DEBUG', 'false').lower() == 'true', host='0.0.0.0', port=5000)

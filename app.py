from flask import Flask, request
from flask_cors import CORS
from flask_jwt_extended import JWTManager
from utils.config import AppConfig
import logging
import os

# Load environment variables
try:
    from pathlib import Path
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parents[2] / '.env')
except ImportError:
    pass

# Configure logging
handlers = [logging.StreamHandler()]

if os.getenv('USE_SSM_SECRETS', 'false').lower() != 'true':
    handlers.append(logging.FileHandler('app.log'))

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s:%(levelname)s:%(name)s:%(message)s',
    handlers=handlers
)

logger = logging.getLogger(__name__)


def create_app():
    """
    Application factory for the jobs-resume microservice.

    Registers only the jobs and resume blueprints.
    """
    app = Flask(__name__)

    # Configuration
    app.config['JWT_SECRET_KEY'] = AppConfig.JWT_SECRET_KEY
    app.config['JWT_ACCESS_TOKEN_EXPIRES'] = AppConfig.JWT_ACCESS_TOKEN_EXPIRES

    # Security: Restrict CORS to specific origins in production
    allowed_origins = os.getenv('ALLOWED_ORIGINS', 'http://localhost:5173,http://localhost:5174,http://localhost:3000').split(',')
    CORS(app, resources={
        r"/api/*": {
            "origins": allowed_origins,
            "methods": ["GET", "POST", "PUT", "DELETE", "OPTIONS"],
            "allow_headers": ["Content-Type", "Authorization"],
            "expose_headers": ["Content-Disposition", "X-Trace-Id", "X-Amzn-Trace-Id", "Server-Timing"],
            "supports_credentials": True,
            "max_age": 3600
        }
    })
    jwt = JWTManager(app)

    # X-Ray instrumentation (only in Lambda / production)
    if os.getenv('USE_SSM_SECRETS', 'false').lower() == 'true':
        try:
            from aws_xray_sdk.core import xray_recorder, patch_all
            from aws_xray_sdk.ext.flask.middleware import XRayMiddleware
            xray_recorder.configure(service='jobs-resume-service')
            XRayMiddleware(app, xray_recorder)
            patch_all()
            logger.info("X-Ray SDK initialized — Flask, PyMongo, boto3, requests instrumented")
        except ImportError:
            logger.warning("aws-xray-sdk not installed, skipping X-Ray instrumentation")
        except Exception as e:
            logger.warning(f"X-Ray initialization failed: {e}")

    # Security headers on all responses
    @app.after_request
    def set_security_headers(response):
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['X-XSS-Protection'] = '0'
        response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
        if request.path.startswith('/api/'):
            response.headers['Cache-Control'] = 'no-store'
        return response

    # Register blueprints — jobs and resume only
    from blueprints.jobs import jobs_bp
    app.register_blueprint(jobs_bp, url_prefix='/api/jobs')

    from blueprints.resume import resume_bp
    app.register_blueprint(resume_bp, url_prefix='/api/resume')

    # Mounted UNDER /api/resume rather than a top-level /api/feedback so it
    # rides the existing "ANY /api/resume/{proxy+}" API Gateway route to this
    # Lambda. There is no /api/feedback route in the gateway, so a top-level
    # mount would 404 at the edge and silently drop every submission.
    from blueprints.feedback import feedback_bp
    app.register_blueprint(feedback_bp, url_prefix='/api/resume/feedback')

    from blueprints.tech_chronicle import tech_chronicle_bp
    app.register_blueprint(tech_chronicle_bp, url_prefix='/api/tech-chronicle')

    # Job Application Autofill Chrome extension backend. Mounted at a dedicated
    # top-level /api/apply prefix (NOT under /api/resume) so its traffic shows
    # up as its own route group in CloudWatch and is easy to monitor. This
    # REQUIRES a matching "ANY /api/apply/{proxy+}" route in
    # infrastructure/terraform/lambda.tf — without it the gateway silently
    # returns the SPA index.html for /api/apply/* instead of hitting this Lambda.
    from blueprints.apply import apply_bp
    app.register_blueprint(apply_bp, url_prefix='/api/apply')

    # Health check endpoint
    @app.route('/api/health')
    def health():
        """Health check endpoint for monitoring"""
        return {
            'status': 'healthy',
            'service': 'jobs-resume-service',
            'version': '2.0.0'
        }, 200

    logger.info("Jobs-Resume service initialized with blueprints registered")
    return app


if __name__ == '__main__':
    app = create_app()
    debug = os.getenv('FLASK_DEBUG', 'false').lower() == 'true'
    app.run(debug=debug, host='0.0.0.0', port=5000)

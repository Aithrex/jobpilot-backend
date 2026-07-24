"""Application factory for the RESUME microservice (Lambda).

Owns the core async résumé APIs (parse/tailor/score/render), Tech Chronicle,
and the feedback endpoint. This is the ONLY Lambda that runs the async-job
processor (services.resume_service.process_async_job, dispatched via Lambda
self-invocation — see lambda_handler.py).

Edge routes (external Terraform): ANY /api/resume/{proxy+},
ANY /api/tech-chronicle/{proxy+}. The feedback blueprint is intentionally
mounted UNDER /api/resume/feedback so it rides the existing /api/resume route
(the frontend calls it there) — do NOT promote it to a top-level prefix.

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
            xray_recorder.configure(service='resume-service')
            XRayMiddleware(app, xray_recorder)
            patch_all()
            logger.info("X-Ray SDK initialized for resume-service")
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

    from blueprints.resume import resume_bp
    app.register_blueprint(resume_bp, url_prefix='/api/resume')

    # Mounted UNDER /api/resume so it rides the existing "ANY /api/resume/{proxy+}"
    # edge route (the frontend posts here). A top-level /api/feedback mount would
    # 404 at the gateway and silently drop submissions.
    from blueprints.feedback import feedback_bp
    app.register_blueprint(feedback_bp, url_prefix='/api/resume/feedback')

    from blueprints.tech_chronicle import tech_chronicle_bp
    app.register_blueprint(tech_chronicle_bp, url_prefix='/api/tech-chronicle')

    # Voice Graph — mounted UNDER /api/resume so it rides the existing
    # "ANY /api/resume/{proxy+}" edge route (no new Terraform route). Gated by
    # VOICE_GRAPH_ENABLED (default off in prod → routes 404).
    from blueprints.voice_graph import voice_graph_bp
    app.register_blueprint(voice_graph_bp, url_prefix='/api/resume/voice-graph')

    # Sprint Learning Hub — mounted UNDER /api/resume so it rides the existing
    # "ANY /api/resume/{proxy+}" edge route (no new Terraform route needed).
    from blueprints.sprint_hub import sprint_hub_bp
    app.register_blueprint(sprint_hub_bp, url_prefix='/api/resume/hub')

    @app.route('/api/health')
    def health():
        return {'status': 'healthy', 'service': 'resume-service', 'version': '2.0.0'}, 200

    logger.info("Resume service initialized (resume, feedback, tech_chronicle, voice_graph registered)")
    return app


if __name__ == '__main__':
    app = create_app()
    app.run(debug=os.getenv('FLASK_DEBUG', 'false').lower() == 'true', host='0.0.0.0', port=5000)

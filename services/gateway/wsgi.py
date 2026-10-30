"""Production gateway: every service's blueprints as ONE Flask app.

Replaces the per-service Lambdas on the Oracle box. Same idea as local.py, with
the routes local.py leaves out (learning, mailbox, mail_inbound, executor) and
the request-body cap the service factories set.

    gunicorn -c infra/compose/gunicorn.conf.py services.gateway.wsgi:app

Async jobs run on a bounded pool in this process (ResumeService.invoke_async);
there is no Lambda self-invoke here because AWS_LAMBDA_FUNCTION_NAME is unset.
"""
import logging
import os
import sys
from pathlib import Path

backend_dir = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(backend_dir / 'shared' / 'python'))
for svc in ['auth', 'jobs', 'resume', 'billing', 'learning']:
    sys.path.insert(0, str(backend_dir / 'services' / svc))

from flask import Flask, request
from flask_cors import CORS
from flask_jwt_extended import JWTManager
from utils.config import AppConfig

logging.basicConfig(
    level=getattr(logging, os.getenv('LOG_LEVEL', 'INFO')),
    format='%(asctime)s:%(levelname)s:%(name)s:%(message)s',
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger(__name__)


def create_app():
    app = Flask(__name__)

    app.config['JWT_SECRET_KEY'] = AppConfig.JWT_SECRET_KEY
    app.config['JWT_ACCESS_TOKEN_EXPIRES'] = AppConfig.JWT_ACCESS_TOKEN_EXPIRES
    app.config['MAX_CONTENT_LENGTH'] = 8 * 1024 * 1024

    @app.errorhandler(413)
    def _payload_too_large(_e):
        return {"error": "file_too_large"}, 413

    allowed_origins = os.getenv('ALLOWED_ORIGINS', 'https://aspirely.me').split(',')
    CORS(app, resources={
        r"/api/*": {
            "origins": allowed_origins,
            "methods": ["GET", "POST", "PUT", "DELETE", "OPTIONS"],
            "allow_headers": [
                "Content-Type", "Authorization",
                "X-Guest-Id", "X-Guest-Device-Token",
            ],
            "expose_headers": [
                "Content-Disposition", "X-Trace-Id", "X-Amzn-Trace-Id", "Server-Timing",
                "X-Resume-Pages", "X-Resume-Outcome", "X-Resume-Fill-Ratio", "X-Resume-Dropped",
            ],
            "supports_credentials": True,
            "max_age": 3600,
        }
    })
    JWTManager(app)

    @app.after_request
    def set_security_headers(response):
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['X-XSS-Protection'] = '0'
        response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
        # /api/jobs/logo/* is a public immutable asset — the route sets its
        # own long-lived Cache-Control; everything else stays no-store.
        if request.path.startswith('/api/') and not request.path.startswith('/api/jobs/logo/'):
            response.headers['Cache-Control'] = 'no-store'
        return response

    from blueprints.auth import auth_bp
    app.register_blueprint(auth_bp, url_prefix='/api/auth')
    from blueprints.admin import admin_bp
    app.register_blueprint(admin_bp, url_prefix='/api/admin')

    from blueprints.jobs import jobs_bp
    app.register_blueprint(jobs_bp, url_prefix='/api/jobs')
    from blueprints.apply import apply_bp
    app.register_blueprint(apply_bp, url_prefix='/api/apply')

    from blueprints.resume import resume_bp
    app.register_blueprint(resume_bp, url_prefix='/api/resume')
    from blueprints.feedback import feedback_bp
    app.register_blueprint(feedback_bp, url_prefix='/api/resume/feedback')
    from blueprints.tech_chronicle import tech_chronicle_bp
    app.register_blueprint(tech_chronicle_bp, url_prefix='/api/tech-chronicle')
    from blueprints.voice_graph import voice_graph_bp
    app.register_blueprint(voice_graph_bp, url_prefix='/api/resume/voice-graph')
    from blueprints.sprint_hub import sprint_hub_bp
    app.register_blueprint(sprint_hub_bp, url_prefix='/api/resume/hub')
    from blueprints.guest import guest_bp
    app.register_blueprint(guest_bp, url_prefix='/api/resume/guest')
    from blueprints.mail_inbound import mail_inbound_bp
    app.register_blueprint(mail_inbound_bp, url_prefix='/api/resume/mail-inbound')
    from blueprints.mailbox import mailbox_bp
    app.register_blueprint(mailbox_bp, url_prefix='/api/resume/mailbox')
    from blueprints.executor import executor_bp
    app.register_blueprint(executor_bp, url_prefix='/api/resume/executor')

    from blueprints.billing import billing_bp
    app.register_blueprint(billing_bp, url_prefix='/api/billing')

    from blueprints.learning import learning_bp
    app.register_blueprint(learning_bp, url_prefix='/api/learning')

    # Each service used to answer its own /health under its prefix through the
    # edge; the deploy health gate and CI read the revision from here.
    @app.route('/api/health')
    def health():
        return {
            'status': 'healthy',
            'service': 'gateway',
            'revision': os.getenv('APP_REVISION', 'unknown'),
        }, 200

    logger.info("Gateway initialized: all service blueprints registered")
    return app


app = create_app()

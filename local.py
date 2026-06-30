"""Local development gateway — runs ALL four microservices as ONE Flask app.

Usage:
    python local.py        (or: python run_local.py)

All routes are served at http://localhost:5000/api/*.

How it works: we add the shared layer (`shared/python`) and each service
directory (`services/<svc>`) to sys.path. Python 3.12 PEP 420 namespace
packages let `blueprints` and `services` span multiple directories, so
`from blueprints.auth import auth_bp` and `from services.resume_service import …`
resolve regardless of which service dir (or the shared layer) the file lives in.
In production each service is deployed as its own Lambda with only its own dir
on the task root + the shared layer at /opt/python.

Auth note: this gateway both mints and validates JWTs. JWT_SECRET_KEY must match
the legacy portfolio value (set in .env) — do not rotate.
"""

import sys
import os
from pathlib import Path

# ── Path setup ──────────────────────────────────────────────────────
backend_dir = Path(__file__).resolve().parent
sys.path.insert(0, str(backend_dir / 'shared' / 'python'))
for svc in ['auth', 'jobs', 'resume', 'billing']:
    sys.path.insert(0, str(backend_dir / 'services' / svc))

# ── Load .env ───────────────────────────────────────────────────────
try:
    from dotenv import load_dotenv
    load_dotenv(backend_dir / '.env')
except ImportError:
    pass

# ── Flask app ───────────────────────────────────────────────────────
import logging
from flask import Flask, request
from flask_cors import CORS
from flask_jwt_extended import JWTManager
from utils.config import AppConfig

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s:%(levelname)s:%(name)s:%(message)s',
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger(__name__)


def create_app():
    app = Flask(__name__)

    app.config['JWT_SECRET_KEY'] = AppConfig.JWT_SECRET_KEY
    app.config['JWT_ACCESS_TOKEN_EXPIRES'] = AppConfig.JWT_ACCESS_TOKEN_EXPIRES

    allowed_origins = os.getenv(
        'ALLOWED_ORIGINS',
        'http://localhost:5173,http://localhost:5174,http://localhost:3000,http://127.0.0.1:5173',
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

    @app.after_request
    def set_security_headers(response):
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['X-XSS-Protection'] = '0'
        response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
        if request.path.startswith('/api/'):
            response.headers['Cache-Control'] = 'no-store'
        return response

    # ── Register ALL blueprints (namespace packages span all service dirs) ──
    # auth service
    from blueprints.auth import auth_bp
    app.register_blueprint(auth_bp, url_prefix='/api/auth')
    from blueprints.admin import admin_bp
    app.register_blueprint(admin_bp, url_prefix='/api/admin')

    # jobs service
    from blueprints.jobs import jobs_bp
    app.register_blueprint(jobs_bp, url_prefix='/api/jobs')
    from blueprints.apply import apply_bp
    app.register_blueprint(apply_bp, url_prefix='/api/apply')

    # resume service
    from blueprints.resume import resume_bp
    app.register_blueprint(resume_bp, url_prefix='/api/resume')
    from blueprints.feedback import feedback_bp
    app.register_blueprint(feedback_bp, url_prefix='/api/resume/feedback')
    from blueprints.tech_chronicle import tech_chronicle_bp
    app.register_blueprint(tech_chronicle_bp, url_prefix='/api/tech-chronicle')

    # billing service
    from blueprints.billing import billing_bp
    app.register_blueprint(billing_bp, url_prefix='/api/billing')

    @app.route('/api/health')
    def health():
        return {'status': 'healthy', 'service': 'local-gateway', 'version': '2.0.0'}, 200

    logger.info("Local gateway initialized — all 4 services' blueprints registered")
    return app


app = create_app()

if __name__ == '__main__':
    debug = os.getenv('FLASK_DEBUG', 'true').lower() == 'true'
    port = int(os.getenv('PORT', '5000'))
    app.run(debug=debug, host='0.0.0.0', port=port)

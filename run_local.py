"""Local development runner — thin shim around the multi-service gateway.

Kept so the documented `python run_local.py` command keeps working after the
split into four microservices. All wiring (sys.path for the shared layer + each
service dir, blueprint registration) lives in local.py.

Usage:
    python run_local.py

Routes are served at http://localhost:5000/api/*.
"""

import os
from local import create_app

app = create_app()

if __name__ == "__main__":
    debug = os.getenv("FLASK_DEBUG", "true").lower() == "true"
    port = int(os.getenv("PORT", "5000"))
    app.run(debug=debug, host="0.0.0.0", port=port)

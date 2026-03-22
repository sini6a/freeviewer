import logging
import os
import sys

# Allow running as `python server/server.py` directly
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.app import app, create_app  # noqa: F401
from server.extensions import socketio  # noqa: F401

if __name__ == "__main__":
    logging.getLogger("eventlet.wsgi.server").setLevel(logging.CRITICAL)
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("DEBUG", "false").lower() == "true"
    try:
        socketio.run(app, host="0.0.0.0", port=port, debug=debug)
    except KeyboardInterrupt:
        pass

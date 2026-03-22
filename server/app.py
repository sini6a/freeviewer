import atexit
import logging
import os
from pathlib import Path

import pymysql
import pymysql.cursors
from dotenv import load_dotenv
from flask import Flask, g, render_template, session
from werkzeug.middleware.proxy_fix import ProxyFix

from .db import close_db, init_db, query_one
from .extensions import agent_sids, csrf, socketio
from .utils import fmt_dt, get_setting

load_dotenv(Path(__file__).parent / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("freeviewer")


def create_app() -> Flask:
    app = Flask(__name__)
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    secret = os.environ.get("SECRET_KEY", "").strip()
    if not secret:
        raise RuntimeError("SECRET_KEY environment variable must be set before running in production.")
    app.config["SECRET_KEY"] = secret

    app.config["MYSQL_HOST"]     = os.environ.get("MYSQL_HOST", "127.0.0.1")
    app.config["MYSQL_PORT"]     = int(os.environ.get("MYSQL_PORT", "3306"))
    app.config["MYSQL_USER"]     = os.environ.get("MYSQL_USER", "freeviewer")
    app.config["MYSQL_PASSWORD"] = os.environ.get("MYSQL_PASSWORD", "")
    app.config["MYSQL_DATABASE"] = os.environ.get("MYSQL_DATABASE", "freeviewer")

    # Session cookie security
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_SECURE"]   = os.environ.get("SESSION_COOKIE_SECURE", "true").lower() == "true"

    app.jinja_env.filters["dt"] = fmt_dt
    csrf.init_app(app)
    app.teardown_appcontext(close_db)

    @app.before_request
    def load_logged_in_user():
        g.display_timezone = get_setting("timezone", "UTC") or "UTC"
        user_id = session.get("user_id")
        g.user = None
        if user_id is not None:
            g.user = query_one(
                "SELECT id, username, role, banned, created_at FROM users WHERE id = %s",
                (user_id,),
            )
            if g.user and g.user["banned"]:
                session.clear()
                g.user = None

    # Register blueprints
    from .routes.auth import bp as auth_bp
    from .routes.dashboard import bp as dashboard_bp
    from .routes.devices import bp as devices_bp
    from .routes.users import bp as users_bp
    from .routes.settings import bp as settings_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(devices_bp)
    app.register_blueprint(users_bp)
    app.register_blueprint(settings_bp)

    # Register socket handlers (imports trigger decorator registration)
    from . import sockets  # noqa: F401

    @app.errorhandler(404)
    def not_found(e):
        return render_template("errors/404.html"), 404

    @app.errorhandler(500)
    def server_error(e):
        return render_template("errors/500.html"), 500

    @app.errorhandler(403)
    def forbidden(e):
        return render_template("errors/403.html"), 403

    socketio.init_app(app)

    with app.app_context():
        init_db()

    return app


def _on_shutdown():
    """Mark all connected agents offline when the server process exits."""
    if not agent_sids:
        return
    try:
        db = pymysql.connect(
            host=os.environ.get("MYSQL_HOST", "127.0.0.1"),
            port=int(os.environ.get("MYSQL_PORT", "3306")),
            user=os.environ.get("MYSQL_USER", "freeviewer"),
            password=os.environ.get("MYSQL_PASSWORD", ""),
            database=os.environ.get("MYSQL_DATABASE", "freeviewer"),
            cursorclass=pymysql.cursors.DictCursor,
            charset="utf8mb4",
            autocommit=False,
        )
        with db.cursor() as cur:
            for device_code in list(agent_sids.keys()):
                cur.execute(
                    "UPDATE devices SET status = 'offline' WHERE device_code = %s",
                    (device_code,),
                )
        db.commit()
        db.close()
    except Exception:
        pass
    agent_sids.clear()


atexit.register(_on_shutdown)

app = create_app()

if __name__ == "__main__":
    logging.getLogger("eventlet.wsgi.server").setLevel(logging.CRITICAL)
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("DEBUG", "false").lower() == "true"
    try:
        socketio.run(app, host="0.0.0.0", port=port, debug=debug)
    except KeyboardInterrupt:
        pass

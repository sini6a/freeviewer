import atexit
import os
import secrets
import sqlite3
from functools import wraps
from pathlib import Path
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from flask import (
    Flask,
    flash,
    g,
    current_app,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_socketio import SocketIO, emit
from werkzeug.security import check_password_hash, generate_password_hash

load_dotenv(Path(__file__).parent / ".env")

BASE_DIR = Path(__file__).resolve().parent
DATABASE_PATH = BASE_DIR / "freeviewer.db"
PAIRING_CODE_LENGTH = int(os.environ.get("PAIRING_CODE_LENGTH", 6))
PAIRING_EXPIRY_MINUTES = int(os.environ.get("PAIRING_EXPIRY_MINUTES", 10))

socketio = SocketIO()

# Maps device_code -> Socket.IO session id for connected agents
agent_sids = {}


def _fmt_dt(value):
    """Format an ISO timestamp string as '21 Mar 2026, 14:06'."""
    if not value:
        return "—"
    try:
        dt = datetime.fromisoformat(str(value))
        return dt.strftime("%d %b %Y, %H:%M").lstrip("0")
    except Exception:
        return str(value)


def create_app() -> Flask:
    app = Flask(__name__)
    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY") or "change-me-in-production"
    db_url = os.environ.get("DATABASE_URL", "").strip()
    app.config["DATABASE"] = db_url if db_url else str(DATABASE_PATH)
    app.jinja_env.filters["dt"] = _fmt_dt

    @app.before_request
    def load_logged_in_user():
        user_id = session.get("user_id")
        g.user = None
        if user_id is not None:
            g.user = query_one(
                "SELECT id, email, role, created_at FROM users WHERE id = ?",
                (user_id,),
            )

    @app.route("/")
    def index():
        if g.user:
            return redirect(url_for("dashboard"))
        return redirect(url_for("login"))

    @app.route("/login", methods=("GET", "POST"))
    def login():
        if g.user:
            return redirect(url_for("dashboard"))

        has_users = query_one("SELECT id FROM users LIMIT 1") is not None

        if request.method == "POST":
            email = request.form.get("email", "").strip().lower()
            password = request.form.get("password", "")

            user = query_one(
                "SELECT id, email, password_hash FROM users WHERE email = ?",
                (email,),
            )

            if not user or not check_password_hash(user["password_hash"], password):
                flash("Invalid email or password.", "error")
            else:
                session.clear()
                session["user_id"] = user["id"]
                flash("Welcome back.", "success")
                return redirect(url_for("dashboard"))

        return render_template("login.html", has_users=has_users)

    @app.route("/register", methods=("GET", "POST"))
    def register():
        existing_user = query_one("SELECT id FROM users LIMIT 1")
        if existing_user:
            flash("Registration is disabled after the first account is created.", "error")
            return redirect(url_for("login"))

        if request.method == "POST":
            email = request.form.get("email", "").strip().lower()
            password = request.form.get("password", "")
            confirm_password = request.form.get("confirm_password", "")

            if not email or not password:
                flash("Email and password are required.", "error")
            elif password != confirm_password:
                flash("Passwords do not match.", "error")
            else:
                execute(
                    "INSERT INTO users (email, password_hash, role) VALUES (?, ?, 'admin')",
                    (email, generate_password_hash(password)),
                )
                flash("Admin account created. You can sign in now.", "success")
                return redirect(url_for("login"))

        return render_template("register.html")

    @app.route("/logout", methods=("POST",))
    def logout():
        session.clear()
        flash("You have been logged out.", "success")
        return redirect(url_for("login"))

    @app.route("/dashboard")
    @login_required
    def dashboard():
        expire_stale_codes()
        is_admin = g.user["role"] == "admin"
        if is_admin:
            devices = query_all(
                """
                SELECT d.id, d.display_name, d.device_code, d.owner_email, d.owner_user_id,
                       d.status, d.pairing_state, d.hostname, d.last_seen_at, d.created_at,
                       u.email AS owner_email_resolved
                FROM devices d
                LEFT JOIN users u ON u.id = d.owner_user_id
                ORDER BY d.created_at DESC
                """
            )
        else:
            devices = query_all(
                """
                SELECT d.id, d.display_name, d.device_code, d.owner_email, d.owner_user_id,
                       d.status, d.pairing_state, d.hostname, d.last_seen_at, d.created_at,
                       u.email AS owner_email_resolved
                FROM devices d
                LEFT JOIN users u ON u.id = d.owner_user_id
                WHERE d.owner_user_id = ?
                ORDER BY d.created_at DESC
                """,
                (g.user["id"],),
            )
        pairing_requests = query_all(
            """
            SELECT pr.id, pr.code, pr.status, pr.expires_at, pr.created_at,
                   u.email AS requested_by_email
            FROM pairing_requests pr
            JOIN users u ON u.id = pr.user_id
            WHERE pr.user_id = ?
            ORDER BY pr.created_at DESC
            LIMIT 8
            """,
            (g.user["id"],),
        ) if not is_admin else query_all(
            """
            SELECT pr.id, pr.code, pr.status, pr.expires_at, pr.created_at,
                   u.email AS requested_by_email
            FROM pairing_requests pr
            JOIN users u ON u.id = pr.user_id
            ORDER BY pr.created_at DESC
            LIMIT 8
            """
        )
        pending_codes = query_one(
            "SELECT COUNT(*) AS count FROM pairing_requests WHERE status = 'pending'"
        )["count"]
        stats = {
            "total": len(devices),
            "online": sum(1 for device in devices if device["status"] == "online"),
            "paired": sum(1 for device in devices if device["pairing_state"] == "paired"),
            "pending": pending_codes,
        }
        return render_template(
            "dashboard.html",
            devices=devices,
            pairing_requests=pairing_requests,
            stats=stats,
        )

    @app.route("/pair", methods=("GET", "POST"))
    @login_required
    def pair():
        expire_stale_codes()
        latest_request = query_one(
            """
            SELECT pr.id, pr.code, pr.status, pr.expires_at, pr.created_at,
                   u.email AS requested_by_email
            FROM pairing_requests pr
            JOIN users u ON u.id = pr.user_id
            WHERE pr.user_id = ?
            ORDER BY pr.created_at DESC
            LIMIT 1
            """,
            (g.user["id"],),
        )

        if request.method == "POST":
            code = generate_pairing_code()
            expires_at = utc_now() + timedelta(minutes=PAIRING_EXPIRY_MINUTES)
            execute(
                """
                INSERT INTO pairing_requests (user_id, code, status, expires_at)
                VALUES (?, ?, 'pending', ?)
                """,
                (g.user["id"], code, expires_at.isoformat()),
            )
            flash("Pairing request created. Share the code with the authorized user at the device.", "success")
            return redirect(url_for("pair"))

        pending_requests = query_all(
            """
            SELECT pr.id, pr.code, pr.status, pr.expires_at, pr.created_at,
                   u.email AS requested_by_email
            FROM pairing_requests pr
            JOIN users u ON u.id = pr.user_id
            ORDER BY pr.created_at DESC
            """
        )

        return render_template(
            "pair.html",
            latest_request=latest_request,
            pending_requests=pending_requests,
            expiry_minutes=PAIRING_EXPIRY_MINUTES,
        )

    @app.route("/connect/<device_id>")
    @login_required
    def connect(device_id):
        device = query_one(
            "SELECT display_name, device_code, status, pairing_state FROM devices WHERE device_code = ?",
            (device_id,),
        )
        if not device or device["status"] != "online" or device["pairing_state"] != "paired":
            flash("Device is not available for connection.", "error")
            return redirect(url_for("dashboard"))
        return render_template("connect.html", device=device)

    @app.route("/devices/<device_id>/rename", methods=("POST",))
    @login_required
    def rename_device(device_id):
        new_name = request.form.get("display_name", "").strip()
        if not new_name:
            flash("Name cannot be empty.", "error")
            return redirect(url_for("dashboard"))
        execute(
            "UPDATE devices SET display_name = ? WHERE device_code = ?",
            (new_name, device_id),
        )
        flash("Device renamed.", "success")
        return redirect(url_for("dashboard"))

    @app.route("/devices/<device_id>/unblock", methods=("POST",))
    @login_required
    def unblock_device(device_id):
        execute(
            "UPDATE devices SET pairing_state = 'pending' WHERE device_code = ? AND pairing_state = 'blocked'",
            (device_id,),
        )
        flash("Device unblocked. It can now pair again.", "success")
        return redirect(url_for("dashboard"))

    @app.route("/devices/<device_id>/delete", methods=("POST",))
    @login_required
    def delete_device(device_id):
        if device_id in agent_sids:
            socketio.emit("force_disconnect", {"message": "Device removed from server"}, to=agent_sids[device_id])
            del agent_sids[device_id]
        execute("DELETE FROM devices WHERE device_code = ?", (device_id,))
        execute("DELETE FROM pairing_requests WHERE user_id NOT IN (SELECT id FROM users)", ())
        flash("Device deleted.", "success")
        return redirect(url_for("dashboard"))

    @app.route("/devices", methods=("POST",))
    @login_required
    def create_device():
        display_name = request.form.get("display_name", "").strip()
        device_code = request.form.get("device_code", "").strip().lower()
        owner_email = request.form.get("owner_email", "").strip().lower()

        if not display_name or not device_code:
            flash("Device name and device code are required.", "error")
            return redirect(url_for("dashboard"))

        existing = query_one(
            "SELECT id FROM devices WHERE device_code = ?",
            (device_code,),
        )
        if existing:
            flash("That device code already exists.", "error")
            return redirect(url_for("dashboard"))

        execute(
            """
            INSERT INTO devices (display_name, device_code, owner_email, status, pairing_state)
            VALUES (?, ?, ?, 'offline', 'pending')
            """,
            (display_name, device_code, owner_email or None),
        )
        flash("Device saved to the backend inventory.", "success")
        return redirect(url_for("dashboard"))

    # ── Users (admin only) ────────────────────────────────────────────────────

    @app.route("/users")
    @admin_required
    def users():
        all_users = query_all(
            "SELECT id, email, role, created_at FROM users ORDER BY created_at"
        )
        return render_template("users.html", users=all_users)

    @app.route("/users", methods=("POST",))
    @admin_required
    def create_user():
        email    = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        role     = request.form.get("role", "user")
        if not email or not password:
            flash("Email and password are required.", "error")
            return redirect(url_for("users"))
        if query_one("SELECT id FROM users WHERE email = ?", (email,)):
            flash("That email is already registered.", "error")
            return redirect(url_for("users"))
        execute(
            "INSERT INTO users (email, password_hash, role) VALUES (?, ?, ?)",
            (email, generate_password_hash(password), role),
        )
        flash(f"User {email} created.", "success")
        return redirect(url_for("users"))

    @app.route("/users/<int:user_id>/delete", methods=("POST",))
    @admin_required
    def delete_user(user_id):
        if user_id == g.user["id"]:
            flash("You cannot delete your own account.", "error")
            return redirect(url_for("users"))
        execute("DELETE FROM users WHERE id = ?", (user_id,))
        flash("User deleted.", "success")
        return redirect(url_for("users"))

    @app.route("/users/<int:user_id>/toggle-admin", methods=("POST",))
    @admin_required
    def toggle_admin(user_id):
        if user_id == g.user["id"]:
            flash("You cannot change your own role.", "error")
            return redirect(url_for("users"))
        user = query_one("SELECT role FROM users WHERE id = ?", (user_id,))
        if user:
            new_role = "user" if user["role"] == "admin" else "admin"
            execute("UPDATE users SET role = ? WHERE id = ?", (new_role, user_id))
            flash(f"Role updated to {new_role}.", "success")
        return redirect(url_for("users"))

    # ── Settings (admin only) ─────────────────────────────────────────────────

    @app.route("/settings", methods=("GET", "POST"))
    @admin_required
    def settings():
        if request.method == "POST":
            set_setting("turn_url",        request.form.get("turn_url", "").strip())
            set_setting("turn_username",   request.form.get("turn_username", "").strip())
            set_setting("turn_credential", request.form.get("turn_credential", "").strip())
            flash("Settings saved.", "success")
            return redirect(url_for("settings"))
        return render_template(
            "settings.html",
            turn_url=get_setting("turn_url", ""),
            turn_username=get_setting("turn_username", ""),
            turn_credential=get_setting("turn_credential", ""),
        )

    # ── ICE config API ────────────────────────────────────────────────────────

    @app.route("/api/ice-config")
    @login_required
    def ice_config():
        return jsonify({"iceServers": get_ice_servers()})

    socketio.init_app(app)

    with app.app_context():
        init_db()

    return app


@socketio.on('register_agent')
def handle_register_agent(data):
    device_id = data.get('id', '').strip()
    device_name = data.get('name', '').strip()
    pairing_code = data.get('pairing_code', '').strip()
    device_token = data.get('device_token', '').strip()

    now = utc_now()

    # --- Unattended reconnect: validate stored token ---
    if device_token:
        device = query_one(
            "SELECT id, display_name, device_token FROM devices WHERE device_code = ? AND pairing_state = 'paired'",
            (device_id,),
        )
        if not device or device['device_token'] != device_token:
            # Flag this device ID so it cannot re-pair without admin unblocking it
            existing = query_one("SELECT id FROM devices WHERE device_code = ?", (device_id,))
            if existing:
                execute(
                    "UPDATE devices SET pairing_state = 'blocked', status = 'offline' WHERE device_code = ?",
                    (device_id,),
                )
            else:
                execute(
                    """INSERT INTO devices (display_name, device_code, status, pairing_state, last_seen_at)
                       VALUES (?, ?, 'offline', 'blocked', ?)""",
                    (device_name or device_id, device_id, now.isoformat()),
                )
            emit('registration_status', {'success': False, 'paired': False, 'message': 'Invalid device token'})
            return
        execute(
            "UPDATE devices SET status = 'online', last_seen_at = ? WHERE device_code = ?",
            (now.isoformat(), device_id),
        )
        agent_sids[device_id] = request.sid
        socketio.emit('device_update', {'device_code': device_id, 'status': 'online',
                                        'pairing_state': 'paired', 'last_seen_at': now.isoformat()})
        emit('registration_status', {'success': True, 'paired': True, 'message': 'Reconnected'})
        return

    # --- First-time pairing: validate pairing code ---
    if not pairing_code:
        emit('registration_status', {'success': False, 'paired': False, 'message': 'No pairing code or token provided'})
        return

    blocked = query_one(
        "SELECT id FROM devices WHERE device_code = ? AND pairing_state = 'blocked'",
        (device_id,),
    )
    if blocked:
        emit('registration_status', {'success': False, 'paired': False, 'message': 'Device is blocked — contact the server admin'})
        return

    pr = query_one(
        "SELECT id, user_id, status, expires_at FROM pairing_requests WHERE code = ?",
        (pairing_code,),
    )

    if not pr:
        emit('registration_status', {'success': False, 'paired': False, 'message': 'Invalid pairing code'})
        return

    if pr['status'] != 'pending':
        emit('registration_status', {'success': False, 'paired': False, 'message': 'Pairing code already used or expired'})
        return

    try:
        expires_at = datetime.fromisoformat(pr['expires_at'])
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
    except ValueError:
        emit('registration_status', {'success': False, 'paired': False, 'message': 'Malformed expiry timestamp'})
        return

    if now > expires_at:
        execute("UPDATE pairing_requests SET status = 'expired' WHERE id = ?", (pr['id'],))
        emit('registration_status', {'success': False, 'paired': False, 'message': 'Pairing code has expired'})
        return

    new_token = secrets.token_hex(32)

    existing = query_one("SELECT id FROM devices WHERE device_code = ?", (device_id,))
    if existing:
        execute(
            """UPDATE devices SET display_name = ?, owner_user_id = ?, pairing_state = 'paired',
               status = 'online', last_seen_at = ?, device_token = ? WHERE device_code = ?""",
            (device_name, pr['user_id'], now.isoformat(), new_token, device_id),
        )
    else:
        execute(
            """INSERT INTO devices (display_name, device_code, owner_user_id, status, pairing_state, hostname, last_seen_at, device_token)
               VALUES (?, ?, ?, 'online', 'paired', ?, ?, ?)""",
            (device_name, device_id, pr['user_id'], device_name, now.isoformat(), new_token),
        )

    execute("UPDATE pairing_requests SET status = 'completed' WHERE id = ?", (pr['id'],))
    agent_sids[device_id] = request.sid
    socketio.emit('device_update', {'device_code': device_id, 'status': 'online',
                                    'pairing_state': 'paired', 'last_seen_at': now.isoformat()})
    emit('registration_status', {'success': True, 'paired': True, 'message': 'Device paired successfully', 'device_token': new_token})


@socketio.on('signal')
def handle_signal(data):
    target_id = data.get('target_id')
    if not target_id:
        return
    # Browser → Agent: target_id is a device_code; include ICE config
    if target_id in agent_sids:
        emit('signal', {**data, 'sender_id': request.sid,
                        'ice_servers': get_ice_servers()}, to=agent_sids[target_id])
    elif data.get('type') == 'offer':
        # Browser sent an offer but device is not connected — tell the browser
        emit('signal_error', {'message': 'Device is not connected to the server.'})
    else:
        # Agent → Browser: target_id is the browser's socket sid
        emit('signal', data, to=target_id)


@socketio.on('disconnect')
def handle_disconnect():
    sid = request.sid
    for device_code, stored_sid in list(agent_sids.items()):
        if stored_sid == sid:
            del agent_sids[device_code]
            execute(
                "UPDATE devices SET status = 'offline' WHERE device_code = ?",
                (device_code,),
            )
            device = query_one(
                "SELECT pairing_state, last_seen_at FROM devices WHERE device_code = ?",
                (device_code,),
            )
            socketio.emit('device_update', {
                'device_code': device_code,
                'status': 'offline',
                'pairing_state': device['pairing_state'] if device else 'paired',
                'last_seen_at': device['last_seen_at'] if device else None,
            })
            break


def get_db():
    if "db" not in g:
        database_url = current_app.config["DATABASE"]
        g.db = sqlite3.connect(database_url)
        g.db.row_factory = sqlite3.Row
    return g.db


def close_db(_error=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = get_db()
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS devices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            display_name TEXT NOT NULL,
            device_code TEXT NOT NULL UNIQUE,
            owner_email TEXT,
            owner_user_id INTEGER,
            status TEXT NOT NULL DEFAULT 'offline',
            pairing_state TEXT NOT NULL DEFAULT 'pending',
            hostname TEXT,
            os_name TEXT,
            os_version TEXT,
            last_seen_at TIMESTAMP,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (owner_user_id) REFERENCES users (id)
        );

        CREATE TABLE IF NOT EXISTS pairing_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            code TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL DEFAULT 'pending',
            expires_at TIMESTAMP NOT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users (id)
        );

        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT
        );
        """
    )
    ensure_column(db, "devices", "owner_user_id", "INTEGER")
    ensure_column(db, "devices", "hostname", "TEXT")
    ensure_column(db, "devices", "os_name", "TEXT")
    ensure_column(db, "devices", "os_version", "TEXT")
    ensure_column(db, "devices", "device_token", "TEXT")
    ensure_column(db, "users", "role", "TEXT NOT NULL DEFAULT 'user'")
    db.commit()


def query_one(query, params=()):
    return get_db().execute(query, params).fetchone()


def query_all(query, params=()):
    return get_db().execute(query, params).fetchall()


def execute(query, params=()):
    db = get_db()
    db.execute(query, params)
    db.commit()


def ensure_column(db, table_name, column_name, column_definition):
    columns = {
        row["name"]
        for row in db.execute(f"PRAGMA table_info({table_name})").fetchall()
    }
    if column_name not in columns:
        db.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_definition}")


def expire_stale_codes():
    execute(
        "UPDATE pairing_requests SET status = 'expired' WHERE status = 'pending' AND expires_at < ?",
        (utc_now().isoformat(),),
    )


def generate_pairing_code():
    alphabet = "23456789"
    return "".join(secrets.choice(alphabet) for _ in range(PAIRING_CODE_LENGTH))


def utc_now():
    return datetime.now(timezone.utc)


def login_required(view):
    @wraps(view)
    def wrapped_view(**kwargs):
        if g.user is None:
            return redirect(url_for("login"))
        return view(**kwargs)
    return wrapped_view


def admin_required(view):
    @wraps(view)
    def wrapped_view(**kwargs):
        if g.user is None:
            return redirect(url_for("login"))
        if g.user["role"] != "admin":
            flash("Admin access required.", "error")
            return redirect(url_for("dashboard"))
        return view(**kwargs)
    return wrapped_view


def get_setting(key, default=None):
    row = query_one("SELECT value FROM settings WHERE key = ?", (key,))
    return row["value"] if row else default


def set_setting(key, value):
    execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
        (key, value),
    )


def get_ice_servers() -> list:
    servers = [{"urls": "stun:stun.l.google.com:19302"}]
    turn_url  = get_setting("turn_url", "")
    turn_user = get_setting("turn_username", "")
    turn_cred = get_setting("turn_credential", "")
    if turn_url:
        entry = {"urls": turn_url}
        if turn_user:
            entry["username"] = turn_user
        if turn_cred:
            entry["credential"] = turn_cred
        servers.append(entry)
    return servers


app = create_app()
app.teardown_appcontext(close_db)


def _on_shutdown():
    """Mark all connected agents offline when the server process exits."""
    if not agent_sids:
        return
    try:
        db = sqlite3.connect(app.config["DATABASE"])
        for device_code in list(agent_sids.keys()):
            db.execute("UPDATE devices SET status = 'offline' WHERE device_code = ?", (device_code,))
        db.commit()
        db.close()
    except Exception:
        pass
    agent_sids.clear()


atexit.register(_on_shutdown)


if __name__ == "__main__":
    import logging
    logging.getLogger("eventlet.wsgi.server").setLevel(logging.CRITICAL)

    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("DEBUG", "false").lower() == "true"
    try:
        socketio.run(app, host="0.0.0.0", port=port, debug=debug)
    except KeyboardInterrupt:
        pass

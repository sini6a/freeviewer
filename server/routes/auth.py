from flask import Blueprint, flash, g, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from ..auth import check_lockout, record_failed_attempt, reset_login_attempts
from ..db import execute, query_one

bp = Blueprint("auth", __name__)


@bp.route("/login", methods=("GET", "POST"))
def login():
    if g.user:
        return redirect(url_for("dashboard.dashboard"))

    has_users = query_one("SELECT id FROM users LIMIT 1") is not None

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        is_locked, remaining = check_lockout(username)
        if is_locked:
            mins, secs = divmod(remaining, 60)
            flash(
                f"Account locked due to too many failed attempts. Try again in {mins}m {secs}s.",
                "error",
            )
        else:
            user = query_one(
                "SELECT id, username, password_hash, banned FROM users WHERE username = %s",
                (username,),
            )
            if not user or not check_password_hash(user["password_hash"], password):
                ip = request.headers.get("X-Forwarded-For", request.remote_addr)
                record_failed_attempt(username, ip)
                flash("Invalid username or password.", "error")
            elif user["banned"]:
                flash("This account has been banned.", "error")
            else:
                reset_login_attempts(username)
                session.clear()
                session["user_id"] = user["id"]
                flash("Welcome back.", "success")
                return redirect(url_for("dashboard.dashboard"))

    return render_template("login.html", has_users=has_users)


@bp.route("/register", methods=("GET", "POST"))
def register():
    existing_user = query_one("SELECT id FROM users LIMIT 1")
    if existing_user:
        flash("Registration is disabled after the first account is created.", "error")
        return redirect(url_for("auth.login"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")

        if not username or not password:
            flash("Username and password are required.", "error")
        elif password != confirm_password:
            flash("Passwords do not match.", "error")
        else:
            execute(
                "INSERT INTO users (username, password_hash, role) VALUES (%s, %s, 'admin')",
                (username, generate_password_hash(password)),
            )
            flash("Admin account created. You can sign in now.", "success")
            return redirect(url_for("auth.login"))

    return render_template("register.html")


@bp.route("/logout", methods=("POST",))
def logout():
    session.clear()
    flash("You have been logged out.", "success")
    return redirect(url_for("auth.login"))

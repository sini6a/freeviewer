from flask import Blueprint, flash, g, redirect, render_template, request, url_for
from werkzeug.security import generate_password_hash

from ..auth import admin_required, reset_login_attempts
from ..db import execute, query_all, query_one
from ..utils import utc_now

bp = Blueprint("users", __name__)


@bp.route("/users")
@admin_required
def users():
    now = utc_now().isoformat()
    all_users = query_all(
        """
        SELECT u.id, u.username, u.role, u.banned, u.created_at,
               CASE WHEN la.locked_until > %s THEN la.locked_until ELSE NULL END AS locked_until
        FROM users u
        LEFT JOIN login_attempts la ON la.username = u.username
        ORDER BY u.created_at
        """,
        (now,),
    )
    attempt_log = query_all(
        """
        SELECT username, ip_address, attempted_at
        FROM login_attempt_log
        ORDER BY attempted_at DESC
        LIMIT 100
        """
    )
    return render_template("users.html", users=all_users, attempt_log=attempt_log)


@bp.route("/users", methods=("POST",))
@admin_required
def create_user():
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    role = request.form.get("role", "user")
    if not username or not password:
        flash("Username and password are required.", "error")
        return redirect(url_for("users.users"))
    if query_one("SELECT id FROM users WHERE username = %s", (username,)):
        flash("That username is already taken.", "error")
        return redirect(url_for("users.users"))
    execute(
        "INSERT INTO users (username, password_hash, role) VALUES (%s, %s, %s)",
        (username, generate_password_hash(password), role),
    )
    flash(f"User {username} created.", "success")
    return redirect(url_for("users.users"))


@bp.route("/users/<int:user_id>/delete", methods=("POST",))
@admin_required
def delete_user(user_id):
    if user_id == g.user["id"]:
        flash("You cannot delete your own account.", "error")
        return redirect(url_for("users.users"))
    execute("DELETE FROM users WHERE id = %s", (user_id,))
    flash("User deleted.", "success")
    return redirect(url_for("users.users"))


@bp.route("/users/<int:user_id>/toggle-admin", methods=("POST",))
@admin_required
def toggle_admin(user_id):
    if user_id == g.user["id"]:
        flash("You cannot change your own role.", "error")
        return redirect(url_for("users.users"))
    user = query_one("SELECT role FROM users WHERE id = %s", (user_id,))
    if user:
        new_role = "user" if user["role"] == "admin" else "admin"
        execute("UPDATE users SET role = %s WHERE id = %s", (new_role, user_id))
        flash(f"Role updated to {new_role}.", "success")
    return redirect(url_for("users.users"))


@bp.route("/users/<int:user_id>/unlock", methods=("POST",))
@admin_required
def unlock_user(user_id):
    user = query_one("SELECT username FROM users WHERE id = %s", (user_id,))
    if user:
        reset_login_attempts(user["username"])
        flash(f"{user['username']} has been unlocked.", "success")
    return redirect(url_for("users.users"))


@bp.route("/users/<int:user_id>/change-password", methods=("POST",))
@admin_required
def change_password(user_id):
    new_password = request.form.get("new_password", "")
    if not new_password:
        flash("Password cannot be empty.", "error")
        return redirect(url_for("users.users"))
    execute(
        "UPDATE users SET password_hash = %s WHERE id = %s",
        (generate_password_hash(new_password), user_id),
    )
    flash("Password updated.", "success")
    return redirect(url_for("users.users"))


@bp.route("/users/<int:user_id>/toggle-ban", methods=("POST",))
@admin_required
def toggle_ban(user_id):
    if user_id == g.user["id"]:
        flash("You cannot ban your own account.", "error")
        return redirect(url_for("users.users"))
    user = query_one("SELECT banned FROM users WHERE id = %s", (user_id,))
    if user:
        new_banned = 0 if user["banned"] else 1
        execute("UPDATE users SET banned = %s WHERE id = %s", (new_banned, user_id))
        flash("User banned." if new_banned else "User unbanned.", "success")
    return redirect(url_for("users.users"))

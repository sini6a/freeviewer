from datetime import timedelta
from functools import wraps

from flask import flash, g, redirect, session, url_for

from .db import execute, query_one
from .utils import utc_now

_LOCKOUT_DURATIONS = [10, 30, 60, 120]  # minutes per escalation level
_LOCKOUT_THRESHOLD = 5                   # failed attempts before lockout


def login_required(view):
    @wraps(view)
    def wrapped_view(**kwargs):
        if g.user is None:
            return redirect(url_for("auth.login"))
        return view(**kwargs)
    return wrapped_view


def admin_required(view):
    @wraps(view)
    def wrapped_view(**kwargs):
        if g.user is None:
            return redirect(url_for("auth.login"))
        if g.user["role"] != "admin":
            flash("Admin access required.", "error")
            return redirect(url_for("dashboard.dashboard"))
        return view(**kwargs)
    return wrapped_view


def check_lockout(username: str):
    """Return (is_locked, seconds_remaining). Does not modify state."""
    row = query_one(
        "SELECT locked_until FROM login_attempts WHERE username = %s", (username,)
    )
    if not row or not row["locked_until"]:
        return False, 0
    from datetime import datetime
    locked_until = datetime.fromisoformat(row["locked_until"])
    remaining = (locked_until - utc_now()).total_seconds()
    return (remaining > 0, max(0, int(remaining)))


def record_failed_attempt(username: str, ip_address=None):
    """Increment failure counter, escalate lockout when threshold hit, log the event."""
    execute(
        "INSERT INTO login_attempt_log (username, ip_address) VALUES (%s, %s)",
        (username, ip_address),
    )
    row = query_one(
        "SELECT failed_count, lockout_level FROM login_attempts WHERE username = %s",
        (username,),
    )
    if row is None:
        execute(
            "INSERT INTO login_attempts (username, failed_count, lockout_level) VALUES (%s, 1, 0)",
            (username,),
        )
        return

    failed_count = row["failed_count"] + 1
    lockout_level = row["lockout_level"]
    locked_until = None

    if failed_count >= _LOCKOUT_THRESHOLD:
        duration = _LOCKOUT_DURATIONS[min(lockout_level, len(_LOCKOUT_DURATIONS) - 1)]
        locked_until = (utc_now() + timedelta(minutes=duration)).isoformat()
        lockout_level += 1
        failed_count = 0

    execute(
        """UPDATE login_attempts
           SET failed_count = %s, locked_until = %s, lockout_level = %s
           WHERE username = %s""",
        (failed_count, locked_until, lockout_level, username),
    )


def reset_login_attempts(username: str):
    execute("DELETE FROM login_attempts WHERE username = %s", (username,))

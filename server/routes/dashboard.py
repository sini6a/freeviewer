from datetime import timedelta

from flask import Blueprint, flash, g, redirect, render_template, request, url_for

from ..auth import login_required
from ..db import execute, query_all, query_one
from ..utils import PAIRING_EXPIRY_MINUTES, expire_stale_codes, generate_pairing_code, utc_now

bp = Blueprint("dashboard", __name__)


@bp.route("/")
def index():
    if g.user:
        return redirect(url_for("dashboard.dashboard"))
    return redirect(url_for("auth.login"))


@bp.route("/dashboard")
@login_required
def dashboard():
    expire_stale_codes()
    is_admin = g.user["role"] == "admin"
    if is_admin:
        devices = query_all(
            """
            SELECT d.id, d.display_name, d.device_code, d.owner_email, d.owner_user_id,
                   d.status, d.pairing_state, d.hostname, d.last_seen_at, d.created_at,
                   u.username AS owner_username_resolved
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
                   u.username AS owner_username_resolved
            FROM devices d
            LEFT JOIN users u ON u.id = d.owner_user_id
            WHERE d.owner_user_id = %s
            ORDER BY d.created_at DESC
            """,
            (g.user["id"],),
        )
    if is_admin:
        pairing_requests = query_all(
            """
            SELECT pr.id, pr.code, pr.status, pr.expires_at, pr.created_at,
                   u.username AS requested_by_username
            FROM pairing_requests pr
            JOIN users u ON u.id = pr.user_id
            ORDER BY pr.created_at DESC
            LIMIT 8
            """
        )
    else:
        pairing_requests = query_all(
            """
            SELECT pr.id, pr.code, pr.status, pr.expires_at, pr.created_at,
                   u.username AS requested_by_username
            FROM pairing_requests pr
            JOIN users u ON u.id = pr.user_id
            WHERE pr.user_id = %s
            ORDER BY pr.created_at DESC
            LIMIT 8
            """,
            (g.user["id"],),
        )
    pending_codes = query_one(
        "SELECT COUNT(*) AS count FROM pairing_requests WHERE status = 'pending'"
    )["count"]
    stats = {
        "total": len(devices),
        "online": sum(1 for d in devices if d["status"] == "online"),
        "paired": sum(1 for d in devices if d["pairing_state"] == "paired"),
        "pending": pending_codes,
    }
    return render_template(
        "dashboard.html",
        devices=devices,
        pairing_requests=pairing_requests,
        stats=stats,
    )


@bp.route("/pair", methods=("GET", "POST"))
@login_required
def pair():
    expire_stale_codes()
    latest_request = query_one(
        """
        SELECT pr.id, pr.code, pr.status, pr.expires_at, pr.created_at,
               u.username AS requested_by_username
        FROM pairing_requests pr
        JOIN users u ON u.id = pr.user_id
        WHERE pr.user_id = %s
        ORDER BY pr.created_at DESC
        LIMIT 1
        """,
        (g.user["id"],),
    )

    if request.method == "POST":
        code = generate_pairing_code()
        expires_at = utc_now() + timedelta(minutes=PAIRING_EXPIRY_MINUTES)
        execute(
            "INSERT INTO pairing_requests (user_id, code, status, expires_at) VALUES (%s, %s, 'pending', %s)",
            (g.user["id"], code, expires_at.isoformat()),
        )
        flash("Pairing request created. Share the code with the authorized user at the device.", "success")
        return redirect(url_for("dashboard.pair"))

    pending_requests = query_all(
        """
        SELECT pr.id, pr.code, pr.status, pr.expires_at, pr.created_at,
               u.username AS requested_by_username
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


@bp.route("/connect/<device_id>")
@login_required
def connect(device_id):
    device = query_one(
        "SELECT display_name, device_code, status, pairing_state FROM devices WHERE device_code = %s",
        (device_id,),
    )
    if not device or device["status"] != "online" or device["pairing_state"] != "paired":
        flash("Device is not available for connection.", "error")
        return redirect(url_for("dashboard.dashboard"))
    return render_template("connect.html", device=device)

from flask import Blueprint, flash, g, redirect, url_for, request

from ..auth import login_required
from ..db import execute, query_one
from ..extensions import agent_sids, socketio

bp = Blueprint("devices", __name__)


@bp.route("/devices", methods=("POST",))
@login_required
def create_device():
    display_name = request.form.get("display_name", "").strip()
    device_code = request.form.get("device_code", "").strip().lower()
    owner_email = request.form.get("owner_email", "").strip().lower()

    if not display_name or not device_code:
        flash("Device name and device code are required.", "error")
        return redirect(url_for("dashboard.dashboard"))

    if query_one("SELECT id FROM devices WHERE device_code = %s", (device_code,)):
        flash("That device code already exists.", "error")
        return redirect(url_for("dashboard.dashboard"))

    execute(
        "INSERT INTO devices (display_name, device_code, owner_email, status, pairing_state) VALUES (%s, %s, %s, 'offline', 'pending')",
        (display_name, device_code, owner_email or None),
    )
    flash("Device saved to the backend inventory.", "success")
    return redirect(url_for("dashboard.dashboard"))


@bp.route("/devices/<device_id>/rename", methods=("POST",))
@login_required
def rename_device(device_id):
    new_name = request.form.get("display_name", "").strip()
    if not new_name:
        flash("Name cannot be empty.", "error")
        return redirect(url_for("dashboard.dashboard"))
    execute(
        "UPDATE devices SET display_name = %s WHERE device_code = %s",
        (new_name, device_id),
    )
    flash("Device renamed.", "success")
    return redirect(url_for("dashboard.dashboard"))


@bp.route("/devices/<device_id>/unblock", methods=("POST",))
@login_required
def unblock_device(device_id):
    execute(
        "UPDATE devices SET pairing_state = 'pending' WHERE device_code = %s AND pairing_state = 'blocked'",
        (device_id,),
    )
    flash("Device unblocked. It can now pair again.", "success")
    return redirect(url_for("dashboard.dashboard"))


@bp.route("/devices/<device_id>/delete", methods=("POST",))
@login_required
def delete_device(device_id):
    if device_id in agent_sids:
        socketio.emit("force_disconnect", {"message": "Device removed from server"}, to=agent_sids[device_id])
        del agent_sids[device_id]
    execute("DELETE FROM devices WHERE device_code = %s", (device_id,))
    execute("DELETE FROM pairing_requests WHERE user_id NOT IN (SELECT id FROM users)", ())
    flash("Device deleted.", "success")
    return redirect(url_for("dashboard.dashboard"))

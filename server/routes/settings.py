try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo

from flask import Blueprint, flash, jsonify, redirect, render_template, request, url_for

from ..auth import admin_required, login_required
from ..utils import get_ice_servers, get_setting, set_setting

bp = Blueprint("settings", __name__)


@bp.route("/settings", methods=("GET", "POST"))
@admin_required
def settings():
    if request.method == "POST":
        tz = request.form.get("timezone", "UTC").strip()
        try:
            ZoneInfo(tz)
        except Exception:
            tz = "UTC"
            flash("Invalid timezone — reset to UTC.", "error")
        set_setting("timezone", tz)
        set_setting("agent_resolution", request.form.get("agent_resolution", "native"))
        set_setting("agent_fps", request.form.get("agent_fps", "30"))
        codec = request.form.get("video_codec", "h264")
        if codec not in ("h264", "vp8"):
            codec = "h264"
        set_setting("video_codec", codec)
        set_setting("stun_url", request.form.get("stun_url", "").strip())
        set_setting("turn_url", request.form.get("turn_url", "").strip())
        set_setting("turn_username", request.form.get("turn_username", "").strip())
        set_setting("turn_credential", request.form.get("turn_credential", "").strip())
        flash("Settings saved.", "success")
        return redirect(url_for("settings.settings"))
    return render_template(
        "settings.html",
        timezone=get_setting("timezone", "UTC") or "UTC",
        agent_resolution=get_setting("agent_resolution", "native") or "native",
        agent_fps=int(get_setting("agent_fps", "30") or 30),
        video_codec=get_setting("video_codec", "h264") or "h264",
        stun_url=get_setting("stun_url", ""),
        turn_url=get_setting("turn_url", ""),
        turn_username=get_setting("turn_username", ""),
        turn_credential=get_setting("turn_credential", ""),
    )


@bp.route("/api/ice-config")
@login_required
def ice_config():
    return jsonify({
        "iceServers": get_ice_servers(),
        "preferredCodec": get_setting("video_codec", "h264") or "h264",
    })

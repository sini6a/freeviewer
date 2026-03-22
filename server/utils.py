import os
import secrets
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo

from .db import execute, query_one

PAIRING_CODE_LENGTH = int(os.environ.get("PAIRING_CODE_LENGTH", 6))
PAIRING_EXPIRY_MINUTES = int(os.environ.get("PAIRING_EXPIRY_MINUTES", 10))

_DEFAULT_STUN = "stun:stun.l.google.com:19302"


def utc_now():
    return datetime.now(timezone.utc)


def fmt_dt(value):
    """Jinja filter: format an ISO/datetime value in the configured timezone."""
    if not value:
        return "—"
    try:
        from flask import g, has_request_context
        dt = datetime.fromisoformat(str(value))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        tz_name = (
            g.display_timezone
            if has_request_context() and hasattr(g, "display_timezone")
            else "UTC"
        )
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            tz = timezone.utc
        return dt.astimezone(tz).strftime("%d %b %Y, %H:%M").lstrip("0")
    except Exception:
        return str(value)


def get_setting(key, default=None):
    row = query_one("SELECT value FROM settings WHERE `key` = %s", (key,))
    return row["value"] if row else default


def set_setting(key, value):
    execute(
        "INSERT INTO settings (`key`, value) VALUES (%s, %s) "
        "ON DUPLICATE KEY UPDATE value = VALUES(value)",
        (key, value),
    )


def expire_stale_codes():
    execute(
        "UPDATE pairing_requests SET status = 'expired' WHERE status = 'pending' AND expires_at < %s",
        (utc_now().isoformat(),),
    )


def generate_pairing_code():
    alphabet = "23456789"
    return "".join(secrets.choice(alphabet) for _ in range(PAIRING_CODE_LENGTH))


def get_capture_settings() -> dict:
    res = get_setting("agent_resolution", "native")
    fps = int(get_setting("agent_fps", "30") or 30)
    resolution_map = {
        "hd":     (1280, 720),
        "fhd":    (1920, 1080),
        "2k":     (2560, 1440),
        "native": (3840, 2160),
    }
    max_w, max_h = resolution_map.get(res, (3840, 2160))
    return {"max_width": max_w, "max_height": max_h, "fps": fps}


def get_ice_servers() -> list:
    stun_url = get_setting("stun_url", "") or _DEFAULT_STUN
    servers = [{"urls": stun_url}]
    turn_url = get_setting("turn_url", "")
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

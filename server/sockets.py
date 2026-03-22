import logging
import secrets
from datetime import datetime, timezone

from flask import request
from flask_socketio import emit

from .db import execute, query_one
from .extensions import agent_sids, socketio
from .utils import get_capture_settings, get_ice_servers, utc_now

log = logging.getLogger("freeviewer")


@socketio.on("register_agent")
def handle_register_agent(data):
    device_id = data.get("id", "").strip()
    device_name = data.get("name", "").strip()
    pairing_code = data.get("pairing_code", "").strip()
    device_token = data.get("device_token", "").strip()

    now = utc_now()

    log.info(
        "register_agent: id=%s name=%s token=%s code=%s sid=%s",
        device_id, device_name,
        "yes" if device_token else "no",
        "yes" if pairing_code else "no",
        request.sid[:8],
    )

    # --- Unattended reconnect: validate stored token ---
    if device_token:
        device = query_one(
            "SELECT id, display_name, device_token FROM devices "
            "WHERE device_code = %s AND pairing_state = 'paired'",
            (device_id,),
        )
        if not device or device["device_token"] != device_token:
            existing = query_one(
                "SELECT id FROM devices WHERE device_code = %s", (device_id,)
            )
            if existing:
                execute(
                    "UPDATE devices SET pairing_state = 'blocked', status = 'offline' "
                    "WHERE device_code = %s",
                    (device_id,),
                )
            else:
                execute(
                    """INSERT INTO devices (display_name, device_code, status, pairing_state, last_seen_at)
                       VALUES (%s, %s, 'offline', 'blocked', %s)""",
                    (device_name or device_id, device_id, now.isoformat()),
                )
            emit("registration_status", {"success": False, "paired": False, "message": "Invalid device token"})
            return

        execute(
            "UPDATE devices SET status = 'online', last_seen_at = %s WHERE device_code = %s",
            (now.isoformat(), device_id),
        )
        agent_sids[device_id] = request.sid
        log.info("agent reconnected: %s (%s) sid=%s", device_id, device_name, request.sid[:8])
        socketio.emit("device_update", {
            "device_code": device_id,
            "status": "online",
            "pairing_state": "paired",
            "last_seen_at": now.isoformat(),
        })
        emit("registration_status", {"success": True, "paired": True, "message": "Reconnected"})
        return

    # --- First-time pairing: validate pairing code ---
    if not pairing_code:
        emit("registration_status", {"success": False, "paired": False, "message": "No pairing code or token provided"})
        return

    blocked = query_one(
        "SELECT id FROM devices WHERE device_code = %s AND pairing_state = 'blocked'",
        (device_id,),
    )
    if blocked:
        emit("registration_status", {"success": False, "paired": False, "message": "Device is blocked — contact the server admin"})
        return

    pr = query_one(
        "SELECT id, user_id, status, expires_at FROM pairing_requests WHERE code = %s",
        (pairing_code,),
    )
    if not pr:
        emit("registration_status", {"success": False, "paired": False, "message": "Invalid pairing code"})
        return

    if pr["status"] != "pending":
        emit("registration_status", {"success": False, "paired": False, "message": "Pairing code already used or expired"})
        return

    try:
        expires_at = datetime.fromisoformat(pr["expires_at"])
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
    except ValueError:
        emit("registration_status", {"success": False, "paired": False, "message": "Malformed expiry timestamp"})
        return

    if now > expires_at:
        execute("UPDATE pairing_requests SET status = 'expired' WHERE id = %s", (pr["id"],))
        emit("registration_status", {"success": False, "paired": False, "message": "Pairing code has expired"})
        return

    new_token = secrets.token_hex(32)
    existing = query_one("SELECT id FROM devices WHERE device_code = %s", (device_id,))
    if existing:
        execute(
            """UPDATE devices SET display_name = %s, owner_user_id = %s, pairing_state = 'paired',
               status = 'online', last_seen_at = %s, device_token = %s WHERE device_code = %s""",
            (device_name, pr["user_id"], now.isoformat(), new_token, device_id),
        )
    else:
        execute(
            """INSERT INTO devices (display_name, device_code, owner_user_id, status, pairing_state,
               hostname, last_seen_at, device_token)
               VALUES (%s, %s, %s, 'online', 'paired', %s, %s, %s)""",
            (device_name, device_id, pr["user_id"], device_name, now.isoformat(), new_token),
        )

    execute("UPDATE pairing_requests SET status = 'completed' WHERE id = %s", (pr["id"],))
    agent_sids[device_id] = request.sid
    log.info("agent paired: %s (%s) sid=%s", device_id, device_name, request.sid[:8])
    socketio.emit("device_update", {
        "device_code": device_id,
        "status": "online",
        "pairing_state": "paired",
        "last_seen_at": now.isoformat(),
    })
    emit("registration_status", {
        "success": True,
        "paired": True,
        "message": "Device paired successfully",
        "device_token": new_token,
    })


@socketio.on("signal")
def handle_signal(data):
    target_id = data.get("target_id")
    sig_type = data.get("type", "?")
    if not target_id:
        return
    if target_id in agent_sids:
        log.info("signal %s: browser %s → agent %s", sig_type, request.sid[:8], target_id)
        emit(
            "signal",
            {
                **data,
                "sender_id": request.sid,
                "ice_servers": get_ice_servers(),
                "capture_settings": get_capture_settings(),
            },
            to=agent_sids[target_id],
        )
    elif data.get("type") == "offer":
        log.warning(
            "signal offer: agent %s not in agent_sids (connected agents: %s)",
            target_id, list(agent_sids.keys()),
        )
        emit("signal_error", {"message": "Device is not connected to the server."})
    else:
        log.info("signal %s: agent → browser %s", sig_type, target_id[:8])
        emit("signal", data, to=target_id)


@socketio.on("ice_candidate")
def handle_ice_candidate(data):
    target_id = data.get("target_id")
    if not target_id:
        return
    if target_id in agent_sids:
        emit("ice_candidate", {**data, "sender_id": request.sid}, to=agent_sids[target_id])
    else:
        emit("ice_candidate", data, to=target_id)


@socketio.on("disconnect")
def handle_disconnect():
    sid = request.sid
    log.info("disconnect: sid=%s (agents online: %d)", sid[:8], len(agent_sids))
    for device_code, stored_sid in list(agent_sids.items()):
        if stored_sid == sid:
            del agent_sids[device_code]
            log.info("agent offline: %s", device_code)
            execute(
                "UPDATE devices SET status = 'offline' WHERE device_code = %s",
                (device_code,),
            )
            device = query_one(
                "SELECT pairing_state, last_seen_at FROM devices WHERE device_code = %s",
                (device_code,),
            )
            socketio.emit("device_update", {
                "device_code": device_code,
                "status": "offline",
                "pairing_state": device["pairing_state"] if device else "paired",
                "last_seen_at": device["last_seen_at"] if device else None,
            })
            break

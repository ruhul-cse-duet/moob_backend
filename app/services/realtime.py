"""Live delivery for conversations.

Messages already persist correctly; what was missing was knowing about one
without asking. The app polled nothing, so a reply only appeared when the
screen was reopened.

This is a Socket.IO server mounted alongside the API. It carries no
authority of its own: a client is told about a message only if it is in a room
it was allowed to join, and joining is checked against the same
`messages.service.contact_ids` rules the HTTP routes use. Nothing is written
here — the REST endpoint remains the only way to create a message, so a dropped
socket can never lose one.

Rooms
  user:<id>     every session that account has open, for badge counts
  thread:<id>   everyone currently looking at one conversation
"""
import logging
from typing import Any, Dict, Optional

import socketio
from fastapi.encoders import jsonable_encoder

from app.core.security import decode_token
from app.db.mongo import tenant_db

logger = logging.getLogger("app.realtime")

#: Same-origin is not a thing for a mobile app, and the token is checked on
#: connect regardless, so the handshake is not where access is decided.
sio = socketio.AsyncServer(async_mode="asgi", cors_allowed_origins="*",
                           logger=False, engineio_logger=False)


def user_room(user_id: str) -> str:
    return f"user:{user_id}"


def thread_room(thread_id: str) -> str:
    return f"thread:{thread_id}"


async def _identify(auth: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The account behind a handshake, or None if the token does not hold up."""
    token = (auth or {}).get("token") or ""
    if not token:
        return None
    try:
        payload = decode_token(token, expected_type="access")
    except ValueError:
        return None
    user_id = payload.get("sub")
    if not user_id:
        return None
    return {"id": str(user_id), "tenant_id": payload.get("tenant_id")}


@sio.event
async def connect(sid: str, environ: Dict[str, Any],
                  auth: Optional[Dict[str, Any]] = None) -> None:
    identity = await _identify(auth)
    if identity is None:
        # Refusing here is what keeps an unauthenticated socket from ever
        # joining a room.
        raise socketio.exceptions.ConnectionRefusedError("Authentication failed")

    await sio.save_session(sid, identity)
    # Its own room, so a notification can reach every device this person has
    # open without knowing anything about their sockets.
    await sio.enter_room(sid, user_room(identity["id"]))
    logger.debug("Socket %s connected as %s", sid, identity["id"])


@sio.event
async def join_thread(sid: str, data: Dict[str, Any]) -> Dict[str, Any]:
    """Starts live delivery for one conversation, if the caller is in it.

    Membership is read from the thread itself rather than trusted from the
    client — otherwise anyone could name a room and listen to it.
    """
    identity = await sio.get_session(sid)
    thread_id = str((data or {}).get("thread_id") or "")
    tenant_id = identity.get("tenant_id")
    if not thread_id or not tenant_id:
        return {"ok": False, "error": "Unknown conversation"}

    from app.core.utils import oid  # local: keeps this module import-light

    db = tenant_db(tenant_id)
    thread = await db.threads.find_one({"_id": oid(thread_id)},
                                       {"participant_ids": 1})
    if not thread or identity["id"] not in thread.get("participant_ids", []):
        return {"ok": False, "error": "You are not part of this conversation"}

    await sio.enter_room(sid, thread_room(thread_id))
    return {"ok": True}


@sio.event
async def leave_thread(sid: str, data: Dict[str, Any]) -> Dict[str, Any]:
    thread_id = str((data or {}).get("thread_id") or "")
    if thread_id:
        await sio.leave_room(sid, thread_room(thread_id))
    return {"ok": True}


@sio.event
async def disconnect(sid: str) -> None:
    logger.debug("Socket %s disconnected", sid)


async def publish_message(message: Dict[str, Any], recipients: list[str]) -> None:
    """Tells the conversation, and the people in it, that a message landed.

    Best effort by design: the message is already stored before this runs, and
    a socket failure must never turn a delivered message into an error.
    """
    thread_id = message.get("thread_id")
    # Socket.IO serialises with plain json.dumps, which cannot encode the
    # datetimes `serialize` leaves in place. Using FastAPI's encoder means the
    # payload arriving on the socket is byte-for-byte what the REST endpoint
    # returns, so the app parses both with the same model.
    payload = jsonable_encoder(message)
    try:
        if thread_id:
            await sio.emit("message", payload, room=thread_room(str(thread_id)))
        for user_id in recipients:
            # Reaches the people not currently looking at the thread, so a
            # badge can move without the conversation being open.
            await sio.emit("message_notice", payload, room=user_room(user_id))
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not publish a message over the socket: %s", exc)

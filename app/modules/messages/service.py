"""Who may talk to whom, and the work behind a conversation.

The router used to take whatever `participant_ids` it was handed and open a
thread. Nothing checked the pairing, so a client could start a conversation with
any user id they could guess - another firm's consultant, or another client.

Messaging follows the working relationship instead:

  * a client talks to the consultant who owns their file, and to a partner that
    consultant has delegated their work to;
  * a consultant talks to their own clients and to their partners;
  * a partner talks to the consultants they work with, and to the clients whose
    work was actually delegated to them.

Everything below is built from that one rule.
"""
from typing import Any, Dict, List, Optional, Set

from fastapi import UploadFile

from app.core.deps import CurrentUser
from app.core.enums import CONSULTANT_ROLES, NotificationType, Role
from app.core.exceptions import BadRequest, Forbidden, NotFound
from app.core.utils import oid, serialize, utcnow
from app.schemas.common import PageParams
from app.services import storage
from app.services.events import notify
from app.services.ownership import assigned_client_ids
from app.services.pagination import paginate
from app.services.realtime import publish_message

ATTACHMENTS_BUCKET = "message_attachments"

#: What a participant summary carries into the app.
_PROFILE_FIELDS = {"full_name": 1, "avatar_url": 1, "role": 1}


# ── who this person is allowed to message ──────────────────────────────────
async def contact_ids(db, user: CurrentUser) -> Set[str]:
    """The set of user ids this account may hold a conversation with."""
    if user.role == Role.CLIENT:
        me = await db.users.find_one({"_id": oid(user.id)},
                                     {"consultant_id": 1, "partner_id": 1})
        return {str(v) for v in (me or {}).values() if isinstance(v, str) and v}

    if user.role in CONSULTANT_ROLES:
        ids = {str(u["_id"]) async for u in db.users.find(
            {"$or": [{"consultant_id": user.id}, {"consultant_ids": user.id}]},
            {"_id": 1})}
        # An owner also reaches the consultants working under them.
        if user.role == Role.CONSULTANT_OWNER:
            ids |= {str(u["_id"]) async for u in db.users.find(
                {"role": Role.CONSULTANT.value}, {"_id": 1})}
        ids.discard(user.id)
        return ids

    if user.role == Role.PARTNER:
        me = await db.users.find_one({"_id": oid(user.id)}, {"consultant_ids": 1})
        consultants = {str(c) for c in (me or {}).get("consultant_ids", []) if c}
        clients = set(await assigned_client_ids(db, user.id))
        return consultants | clients

    return set()


async def contacts(db, user: CurrentUser) -> Dict[str, Any]:
    """The message screen's address book, with any existing thread attached.

    One query for the people and one for the threads, rather than a lookup per
    contact - a consultant with fifty clients would otherwise open fifty-one
    connections to draw a single list.
    """
    ids = await contact_ids(db, user)
    if not ids:
        return {"items": []}

    people = {
        str(p["_id"]): p
        async for p in db.users.find({"_id": {"$in": [oid(i) for i in ids]}},
                                     {**_PROFILE_FIELDS, "email": 1})
    }

    threads: Dict[str, Dict[str, Any]] = {}
    async for thread in db.threads.find({"participant_ids": user.id}):
        for pid in thread.get("participant_ids", []):
            if pid != user.id and pid in people:
                threads[pid] = thread

    items = []
    for pid, person in people.items():
        thread = threads.get(pid)
        items.append({
            "id": pid,
            "full_name": person.get("full_name", ""),
            "email": person.get("email"),
            "avatar_url": person.get("avatar_url"),
            "role": person.get("role"),
            "thread_id": str(thread["_id"]) if thread else None,
            "last_message": (thread or {}).get("last_message"),
            "last_message_at": (thread or {}).get("last_message_at"),
            "unread": (thread or {}).get("unread_counts", {}).get(user.id, 0),
        })

    # Live conversations first, then everyone else alphabetically - the list is
    # for continuing a discussion far more often than for starting one.
    items.sort(key=lambda i: (i["last_message_at"] is None,
                              -(i["last_message_at"].timestamp()
                                if i["last_message_at"] else 0),
                              i["full_name"].lower()))
    return {"items": items}


async def _assert_may_message(db, user: CurrentUser, others: List[str]) -> None:
    allowed = await contact_ids(db, user)
    for other in others:
        if other == user.id:
            continue
        if other not in allowed:
            raise Forbidden("You can only message people you work with")


# ── threads ────────────────────────────────────────────────────────────────
async def open_thread(db, user: CurrentUser, participant_ids: List[str],
                      case_id: Optional[str] = None,
                      subject: Optional[str] = None) -> Dict[str, Any]:
    others = [p for p in dict.fromkeys(participant_ids) if p != user.id]
    if not others:
        raise BadRequest("Choose someone to message")
    await _assert_may_message(db, user, others)

    participants = sorted({*others, user.id})
    existing = await db.threads.find_one({"participant_ids": participants,
                                          "case_id": case_id})
    if existing:
        return await _with_participants(db, serialize(existing))

    now = utcnow()
    doc = {
        "participant_ids": participants,
        # The consultant side of the conversation, so a workspace can find its
        # own threads without walking every participant.
        "consultant_id": user.consultant_id or user.id,
        "case_id": case_id,
        "subject": subject,
        "last_message": None,
        "last_message_at": None,
        "unread_counts": {pid: 0 for pid in participants},
        "created_at": now,
        "updated_at": now,
    }
    result = await db.threads.insert_one(doc)
    return await _with_participants(db, serialize({**doc, "_id": result.inserted_id}))


async def _profiles(db, ids: List[str]) -> Dict[str, Dict[str, Any]]:
    if not ids:
        return {}
    return {
        str(p["_id"]): {
            "id": str(p["_id"]),
            "full_name": p.get("full_name", ""),
            "avatar_url": p.get("avatar_url"),
            "role": p.get("role"),
        }
        async for p in db.users.find({"_id": {"$in": [oid(i) for i in ids]}},
                                     _PROFILE_FIELDS)
    }


async def _with_participants(db, thread: Dict[str, Any]) -> Dict[str, Any]:
    people = await _profiles(db, thread.get("participant_ids", []))
    thread["participants"] = [people[i] for i in thread.get("participant_ids", [])
                              if i in people]
    return thread


async def list_threads(db, user: CurrentUser, params: PageParams) -> Dict[str, Any]:
    page = await paginate(db, "threads", {"participant_ids": user.id}, params,
                          sort=[("updated_at", -1)], enrich=False)

    # Every participant across the whole page in one query. The old version ran
    # a lookup per participant per thread.
    everyone = {pid for item in page["items"] for pid in item.get("participant_ids", [])}
    people = await _profiles(db, list(everyone))

    for item in page["items"]:
        item["participants"] = [people[i] for i in item.get("participant_ids", [])
                                if i in people]
        item["unread"] = (item.get("unread_counts") or {}).get(user.id, 0)
    return page


async def _thread_for(db, user: CurrentUser, thread_id: str) -> Dict[str, Any]:
    thread = await db.threads.find_one({"_id": oid(thread_id)})
    if not thread:
        raise NotFound("Conversation not found")
    if user.id not in thread.get("participant_ids", []):
        raise Forbidden("You are not part of this conversation")
    return thread


async def thread_messages(db, user: CurrentUser, thread_id: str,
                          params: PageParams) -> Dict[str, Any]:
    thread = await _thread_for(db, user, thread_id)
    await _mark_read(db, user, thread_id)

    messages = await paginate(db, "messages", {"thread_id": thread_id}, params,
                              sort=[("created_at", 1)], enrich=False)
    people = await _profiles(db, thread.get("participant_ids", []))
    return {
        "thread": serialize(thread),
        "participants": [people[i] for i in thread.get("participant_ids", [])
                         if i in people],
        "consultant_id": thread.get("consultant_id"),
        **messages,
    }


async def _mark_read(db, user: CurrentUser, thread_id: str) -> None:
    await db.messages.update_many(
        {"thread_id": thread_id, "read_by": {"$ne": user.id}},
        {"$addToSet": {"read_by": user.id}})
    await db.threads.update_one({"_id": oid(thread_id)},
                                {"$set": {f"unread_counts.{user.id}": 0}})


async def mark_read(db, user: CurrentUser, thread_id: str) -> Dict[str, Any]:
    await _thread_for(db, user, thread_id)
    await _mark_read(db, user, thread_id)
    return {"detail": "All messages marked as read"}


# ── sending ────────────────────────────────────────────────────────────────
async def send(db, user: CurrentUser, thread_id: str, body: str,
               attachment: Optional[Dict[str, Any]] = None,
               socket_id: Optional[str] = None) -> Dict[str, Any]:
    thread = await _thread_for(db, user, thread_id)

    body = (body or "").strip()
    if not body and not attachment:
        raise BadRequest("Write something, or attach a file")

    now = utcnow()
    doc = {
        "thread_id": thread_id,
        "sender_id": user.id,
        "sender_name": user.raw.get("full_name", ""),
        "sender_avatar": user.raw.get("avatar_url"),
        "sender_role": user.raw.get("role"),
        "body": body,
        "attachment": attachment,
        "read_by": [user.id],
        "delivered": True,
        "created_at": now,
    }
    result = await db.messages.insert_one(doc)
    message_id = str(result.inserted_id)

    recipients = [p for p in thread["participant_ids"] if p != user.id]
    await db.threads.update_one(
        {"_id": oid(thread_id)},
        {"$set": {"last_message": _preview(body, attachment),
                  "last_message_at": now,
                  "updated_at": now},
         "$inc": {f"unread_counts.{pid}": 1 for pid in recipients}})

    if recipients:
        await notify(db, user_ids=recipients, type=NotificationType.MESSAGE_RECEIVED,
                     title_key="notify.message_received",
                     params={"person": user.raw.get("full_name", "")},
                     body=_preview(body, attachment),
                     data={"thread_id": thread_id, "message_id": message_id})

    sent = serialize({**doc, "_id": result.inserted_id})

    # After the write, and deliberately unable to fail it: the message is
    # already saved, so a socket that is down costs live delivery and nothing
    # else. The app falls back to what it fetches when the screen opens.
    #
    # The sender is skipped: they are in this thread's room, so without it the
    # message they just sent arrives back over the socket as well as in this
    # response, and the app shows it twice.
    await publish_message(sent, recipients, skip_sid=socket_id, sender_id=user.id)
    return sent


def _preview(body: str, attachment: Optional[Dict[str, Any]]) -> str:
    """What the thread list shows. An attachment with no caption still reads."""
    if body:
        return body[:140]
    if attachment:
        kind = "Photo" if str(attachment.get("mime", "")).startswith("image/") else "File"
        return f"{kind}: {attachment.get('name', 'attachment')}"
    return ""


async def send_attachment(db, user: CurrentUser, thread_id: str,
                          file: UploadFile, body: str = "",
                          socket_id: Optional[str] = None) -> Dict[str, Any]:
    """Stores the file, then posts it as a message in one round trip."""
    await _thread_for(db, user, thread_id)

    stored = await storage.save_upload(
        db, file,
        bucket_name=ATTACHMENTS_BUCKET,
        metadata={"thread_id": thread_id, "sender_id": user.id},
    )
    attachment = {
        "file_id": stored["file_id"],
        "name": stored.get("original_name") or "attachment",
        "mime": stored.get("mime_type") or "application/octet-stream",
        "size": stored.get("size", 0),
        "kind": stored.get("kind"),
    }
    return await send(db, user, thread_id, body, attachment=attachment,
                      socket_id=socket_id)


async def attachment_for(db, user: CurrentUser, message_id: str) -> Dict[str, Any]:
    """The stored file behind a message, once the caller is shown to be in it."""
    message = await db.messages.find_one({"_id": oid(message_id)})
    if not message or not message.get("attachment"):
        raise NotFound("No attachment on that message")
    await _thread_for(db, user, message["thread_id"])
    return message["attachment"]


async def unread_total(db, user: CurrentUser) -> int:
    total = 0
    async for thread in db.threads.find({"participant_ids": user.id},
                                        {"unread_counts": 1}):
        total += (thread.get("unread_counts") or {}).get(user.id, 0)
    return total

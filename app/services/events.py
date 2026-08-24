"""Notification + activity feed helpers used by every domain module.

`notify` is the one place a notification is created, which makes it the one
place delivery has to be arranged. It writes the record, tells any live socket
about it, and pushes it to the account's registered devices - so a domain
module says "tell these people this" once and all three happen.

The three legs are deliberately independent. The record is the truth and is
written first; the socket is immediacy for an app that is open; push is reach
for an app that is not. A failure in either delivery leg is logged and dropped,
never raised, because a notification that was stored has already done its job.
"""
import logging
from typing import Any, Dict, List, Optional, Sequence

from motor.motor_asyncio import AsyncIOMotorDatabase

from app.core.config import settings
from app.core.enums import NotificationType
from app.core.utils import utcnow
from app.services import push

logger = logging.getLogger("app.events")

#: What a notification carries that names the thing it is about. Whichever of
#: these is present is lifted into the push payload so a tap can open the right
#: screen without the app having to fetch the feed first.
TARGET_KEYS = ("request_id", "case_id", "document_id", "task_id", "thread_id",
               "ticket_id", "announcement_id", "invoice_id", "partner_id")


async def notify(
    db: AsyncIOMotorDatabase,
    *,
    user_ids: List[str],
    type: NotificationType,
    title: str,
    body: str = "",
    data: Optional[Dict[str, Any]] = None,
    collection: str = "notifications",
    id_field: str = "user_id",
    extra: Optional[Dict[str, Any]] = None,
    push_enabled: bool = True,
    collapse_key: Optional[str] = None,
) -> None:
    """Records one update for each of `user_ids`, then delivers it.

    `collection` and `id_field` exist because the platform side keeps its own
    inboxes under different names (`platform_notifications` keyed on `user_id`,
    `admin_notifications` keyed on `admin_id`). Routing those through here
    rather than inserting directly is what stops an inbox from quietly missing
    out on push.

    `extra` is merged as top-level fields, for the handful of records that carry
    a reference of their own beside `data` - a ticket reference, an announcement
    id. Anything in `extra` that names a target is also pushed.
    """
    recipients = [uid for uid in user_ids if uid]
    if not recipients:
        return

    now = utcnow()
    payload = dict(data or {})
    extras = dict(extra or {})
    documents = [
        {
            id_field: uid,
            "type": type.value,
            "title": title,
            "body": body,
            "data": payload,
            **extras,
            "read": False,
            "created_at": now,
        }
        for uid in recipients
    ]
    await db[collection].insert_many(documents)

    # `data` is where the app looks, but a target passed as a top-level extra
    # must reach it too - otherwise a support reply pushes without its ticket.
    targets = {key: extras[key] for key in TARGET_KEYS if extras.get(key)}
    delivery = {**payload, **targets}

    await _publish(recipients, type=type, title=title, body=body, data=delivery)

    if push_enabled:
        push.dispatch(
            recipients,
            title=title,
            body=body,
            data=delivery,
            type=type.value,
            badges=await _badges(db, collection, id_field, recipients),
            collapse_key=collapse_key,
        )


async def _badges(db: AsyncIOMotorDatabase, collection: str, id_field: str,
                  recipients: Sequence[str]) -> Optional[Dict[str, int]]:
    """Unread totals, for the number iOS draws on the app icon.

    One query per recipient, so it is skipped for a broadcast: an announcement
    to every account on the platform does not need an exact badge, and paying
    for thousands of counts to get one would be the wrong trade.
    """
    if len(recipients) > settings.PUSH_BADGE_RECIPIENT_LIMIT:
        return None
    try:
        return {
            uid: await db[collection].count_documents({id_field: uid, "read": False})
            for uid in recipients
        }
    except Exception as exc:  # noqa: BLE001 - a badge is never worth an error
        logger.debug("Could not count unread notifications for a badge: %s", exc)
        return None


async def _publish(recipients: Sequence[str], *, type: NotificationType,
                   title: str, body: str, data: Dict[str, Any]) -> None:
    """Tells any open app about the notification over the existing socket.

    Imported here rather than at module scope: `realtime` pulls in the Socket.IO
    server, and `events` is imported by nearly every domain module.
    """
    try:
        from app.services.realtime import publish_notification

        await publish_notification(
            recipients,
            {"type": type.value, "title": title, "body": body, "data": data},
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("Could not publish a notification over the socket: %s", exc)


async def log_activity(
    db: AsyncIOMotorDatabase,
    *,
    actor_id: Optional[str],
    actor_name: str,
    action: str,
    subject: str,
    case_id: Optional[str] = None,
    request_id: Optional[str] = None,
) -> None:
    await db.activities.insert_one({
        "actor_id": actor_id,
        "actor_name": actor_name,
        "action": action,
        "subject": subject,
        "case_id": case_id,
        "request_id": request_id,
        "created_at": utcnow(),
    })

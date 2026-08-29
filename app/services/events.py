"""Notification + activity feed helpers used by every domain module.

`notify` is the one place a notification is created, which makes it the one
place delivery has to be arranged. It writes the record, tells any live socket
about it, and pushes it to the account's registered devices - so a domain
module says "tell these people this" once and all three happen.

The three legs are deliberately independent. The record is the truth and is
written first; the socket is immediacy for an app that is open; push is reach
for an app that is not. A failure in either delivery leg is logged and dropped,
never raised, because a notification that was stored has already done its job.

Language
--------
A notification is written once and read later, possibly by several people who
do not share a language. So the record stores a **key and its parameters**, not
a sentence, and the words are chosen when someone reads it - the feed renders in
the reader's language, and push renders per recipient before sending, because
push has no read-time to defer to.

Text a person wrote themselves - an announcement, a task title - is passed as
`title`/`body` instead and is never translated. It is already in the language
its author chose.
"""
import logging
from typing import Any, Dict, List, Optional, Sequence

from motor.motor_asyncio import AsyncIOMotorDatabase

from app.core.config import settings
from app.core.enums import NotificationType
from app.core.i18n import DEFAULT_LANGUAGE
from app.core.i18n import normalize as normalize_language
from app.core.i18n import translate
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
    title: Optional[str] = None,
    body: str = "",
    title_key: Optional[str] = None,
    body_key: Optional[str] = None,
    params: Optional[Dict[str, Any]] = None,
    param_keys: Optional[Dict[str, str]] = None,
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

    Pass `title_key` (and `params`) for anything the platform says, so it can be
    read in the reader's own language. Pass `title` only for words a person
    wrote - an announcement, a task title - which are already in the language
    their author chose and must not be translated.
    """
    recipients = [uid for uid in user_ids if uid]
    if not recipients:
        return

    now = utcnow()
    payload = dict(data or {})
    extras = dict(extra or {})

    # The key and its parameters, not a sentence. Two people on the same
    # notification may not share a language, and the one who reads it in six
    # months may have changed theirs since.
    wording: Dict[str, Any] = {}
    if title_key:
        wording["title_key"] = title_key
        if body_key:
            wording["body_key"] = body_key
        if params:
            wording["params"] = params
        if param_keys:
            wording["param_keys"] = param_keys
    else:
        wording["title"] = title or ""
        wording["body"] = body

    documents = [
        {
            id_field: uid,
            "type": type.value,
            **wording,
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

    # Rendered once per language rather than once per person: a hundred clients
    # on one announcement are at most three renderings and three push calls.
    groups = await _by_language(db, collection, id_field, recipients,
                                translated=bool(title_key))
    badges = await _badges(db, collection, id_field, recipients)

    for lang, group in groups.items():
        said = render(wording, lang)
        await _publish(group, type=type, title=said["title"],
                       body=said["body"], data=delivery)
        if push_enabled:
            push.dispatch(
                group,
                title=said["title"],
                body=said["body"],
                data=delivery,
                type=type.value,
                badges=badges,
                collapse_key=collapse_key,
            )


def render(record: Dict[str, Any], lang: str = DEFAULT_LANGUAGE) -> Dict[str, str]:
    """The words of one notification, in one language.

    Falls back to a stored sentence when there is no key. That covers records
    written before notifications carried keys, and the ones whose words a person
    wrote - both of which have text and nothing to translate.
    """
    key = record.get("title_key")
    if not key:
        return {"title": record.get("title") or "", "body": record.get("body") or ""}

    params = dict(record.get("params") or {})
    # Some parameters are themselves things we say - the *kind* of a data
    # request, the *status* of a task. Translating those where the notification
    # is written would bake the writer's language into a record somebody else
    # reads, so they travel as keys and are resolved here, next to the sentence
    # they go into.
    for name, param_key in (record.get("param_keys") or {}).items():
        params[name] = translate(param_key, lang)

    body_key = record.get("body_key")
    return {
        "title": translate(key, lang, **params),
        "body": translate(body_key, lang, **params) if body_key else "",
    }


async def _by_language(db: AsyncIOMotorDatabase, collection: str, id_field: str,
                       recipients: Sequence[str],
                       translated: bool) -> Dict[str, List[str]]:
    """Recipients grouped by the language each of them reads.

    One query for the whole set. When the wording is not translatable there is
    nothing to group by, so everyone goes in one bucket and the delivery legs
    run once.
    """
    if not translated:
        return {DEFAULT_LANGUAGE: list(recipients)}

    groups: Dict[str, List[str]] = {}
    languages: Dict[str, str] = {}
    try:
        # Tenant accounts live in `users`; the platform inboxes belong to
        # administrators. Whichever this database has is the one that answers.
        source = "platform_admins" if collection.startswith("platform") or \
                 collection.startswith("admin") else "users"
        from app.core.utils import oid

        async for row in db[source].find(
            {"_id": {"$in": [oid(uid) for uid in recipients if uid]}},
            {"language": 1},
        ):
            languages[str(row["_id"])] = normalize_language(row.get("language")) \
                or DEFAULT_LANGUAGE
    except Exception as exc:  # noqa: BLE001 - a language lookup is never worth an error
        logger.debug("Could not read recipient languages: %s", exc)

    for uid in recipients:
        groups.setdefault(languages.get(uid, DEFAULT_LANGUAGE), []).append(uid)
    return groups


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

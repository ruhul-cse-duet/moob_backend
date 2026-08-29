"""
Support tickets. [INFERRED from support/HelpSupport.tsx + admin/Helpdesk.tsx]

Tickets are stored in the PLATFORM database with a tenant_id, not in the tenant
database. A super admin needs one queue across every organization; with
database-per-tenant, a tenant-local collection would force a fan-out query over
every organization database on every helpdesk page load.

Partners and clients (and other workspace users) raise tickets here. Creating a
ticket sends an in-app notification to the super admin(s). No email is sent.
"""
from typing import Any, Dict, List, Optional

from app.core.enums import (
    NotificationType,
    TicketActor,
    TicketPriority,
    TicketStatus,
)
from app.core.exceptions import Forbidden, NotFound
from app.core.utils import build_reference, oid, serialize, utcnow
from app.db.mongo import platform_db
from app.schemas.common import PageParams
from app.services.events import notify
from app.services.pagination import paginate


async def _next_reference() -> str:
    doc = await platform_db().counters.find_one_and_update(
        {"name": "ticket"}, {"$inc": {"value": 1}}, upsert=True, return_document=True)
    return build_reference("TKT", 1000 + doc.get("value", 1))


async def _super_admin_ids() -> List[str]:
    """Return the user IDs of active super admin(s) for in-app notifications."""
    ids: List[str] = []
    async for admin in platform_db().platform_admins.find(
        {"status": {"$ne": "suspended"}}, {"_id": 1}
    ):
        ids.append(str(admin["_id"]))
    return ids


async def create_ticket(user, data) -> Dict[str, Any]:
    db = platform_db()
    now = utcnow()
    reference = await _next_reference()
    doc = {
        "reference": reference,
        "subject": data.subject,
        "category": data.category.value,
        "priority": data.priority.value,
        "status": TicketStatus.OPEN.value,
        "tenant_id": user.tenant_id,
        "requester_id": user.id,
        "requester_email": user.email,
        "requester_name": user.raw.get("full_name"),
        "requester_role": user.role.value,
        "assigned_to": None,
        "assigned_to_name": None,
        "case_reference": data.case_reference,
        "last_reply_at": now,
        "resolved_at": None,
        "created_at": now,
        "updated_at": now,
    }
    ticket_id = str((await db.support_tickets.insert_one(doc)).inserted_id)
    await db.ticket_messages.insert_one({
        "ticket_id": ticket_id,
        "author_id": user.id,
        "author_name": user.raw.get("full_name"),
        "actor": TicketActor.CUSTOMER.value,
        "body": data.message,
        "created_at": now,
    })

    # In-app notification, socket and push in one call - no email.
    admin_ids = await _super_admin_ids()
    if admin_ids:
        await notify(
            db,
            user_ids=admin_ids,
            type=NotificationType.SUPPORT_TICKET,
            title_key="notify.support_ticket_created",
            params={"subject": data.subject},
            body=data.message[:200],
            data={"ticket_id": ticket_id, "reference": reference},
            collection="platform_notifications",
            extra={
                "ticket_id": ticket_id,
                "reference": reference,
                "requester_name": user.raw.get("full_name"),
                "requester_role": user.role.value,
            },
        )

    return serialize({**doc, "_id": oid(ticket_id)})


async def list_tickets(params: PageParams, *, user=None, admin: bool = False,
                       status: Optional[TicketStatus] = None,
                       priority: Optional[TicketPriority] = None,
                       tenant_id: Optional[str] = None,
                       assigned_to: Optional[str] = None,
                       search: Optional[str] = None) -> Dict[str, Any]:
    query: Dict[str, Any] = {}
    if not admin:
        query["requester_id"] = user.id
    if status:
        query["status"] = status.value
    if priority:
        query["priority"] = priority.value
    if tenant_id:
        query["tenant_id"] = tenant_id
    if assigned_to:
        query["assigned_to"] = assigned_to
    if search:
        query["$or"] = [{"reference": {"$regex": search, "$options": "i"}},
                        {"subject": {"$regex": search, "$options": "i"}},
                        {"requester_email": {"$regex": search, "$options": "i"}}]
    return await paginate(platform_db(), "support_tickets", query, params,
                          sort=[("last_reply_at", -1)])


async def get_ticket(ticket_id: str, *, user=None, admin: bool = False) -> Dict[str, Any]:
    db = platform_db()
    ticket = await db.support_tickets.find_one({"_id": oid(ticket_id)})
    if not ticket:
        raise NotFound("Ticket not found")
    if not admin and ticket["requester_id"] != user.id:
        raise Forbidden("This ticket is not yours")
    out = serialize(ticket)
    out["messages"] = [serialize(m) async for m in
                       db.ticket_messages.find({"ticket_id": ticket_id}).sort("created_at", 1)]
    return out


async def reply(ticket_id: str, body: str, *, user, admin: bool = False) -> Dict[str, Any]:
    db = platform_db()
    ticket = await db.support_tickets.find_one({"_id": oid(ticket_id)})
    if not ticket:
        raise NotFound("Ticket not found")
    if not admin and ticket["requester_id"] != user.id:
        raise Forbidden("This ticket is not yours")

    now = utcnow()
    await db.ticket_messages.insert_one({
        "ticket_id": ticket_id,
        "author_id": user.id,
        "author_name": user.raw.get("full_name"),
        "actor": TicketActor.AGENT.value if admin else TicketActor.CUSTOMER.value,
        "body": body,
        "created_at": now,
    })
    new_status = (TicketStatus.WAITING_ON_CUSTOMER.value if admin
                  else TicketStatus.IN_PROGRESS.value)
    await db.support_tickets.update_one(
        {"_id": oid(ticket_id)},
        {"$set": {"last_reply_at": now, "status": new_status, "updated_at": now}})

    # In-app notification instead of email.
    if admin:
        # Admin replied → notify the requester.
        from app.db.mongo import tenant_db
        if ticket.get("tenant_id"):
            await notify(
                tenant_db(ticket["tenant_id"]),
                user_ids=[ticket["requester_id"]],
                type=NotificationType.SUPPORT_REPLY,
                title_key="notify.support_ticket_reply",
                params={"reference": ticket["reference"],
                        "subject": ticket["subject"]},
                body=body[:200],
                data={"ticket_id": ticket_id,
                      "reference": ticket.get("reference")},
                extra={"ticket_id": ticket_id,
                       "reference": ticket.get("reference")},
                # A back-and-forth on one ticket is one running alert rather
                # than a new row in the shade per reply.
                collapse_key=f"ticket:{ticket_id}",
            )
    else:
        # Customer replied → notify admin(s).
        admin_ids = await _super_admin_ids()
        if admin_ids:
            await notify(
                db,
                user_ids=admin_ids,
                type=NotificationType.SUPPORT_REPLY,
                title_key="notify.support_ticket_reply",
                params={"reference": ticket["reference"],
                        "subject": ticket["subject"]},
                body=body[:200],
                data={"ticket_id": ticket_id,
                      "reference": ticket.get("reference")},
                collection="platform_notifications",
                extra={"ticket_id": ticket_id,
                       "reference": ticket.get("reference"),
                       "requester_name": user.raw.get("full_name")},
                collapse_key=f"ticket:{ticket_id}",
            )
    return await get_ticket(ticket_id, user=user, admin=admin)


async def set_status(ticket_id: str, status: TicketStatus, user) -> Dict[str, Any]:
    now = utcnow()
    update = {"status": status.value, "updated_at": now}
    if status in {TicketStatus.RESOLVED, TicketStatus.CLOSED}:
        update["resolved_at"] = now
        update["resolved_by"] = user.id
    result = await platform_db().support_tickets.find_one_and_update(
        {"_id": oid(ticket_id)}, {"$set": update}, return_document=True)
    if not result:
        raise NotFound("Ticket not found")
    return serialize(result)


async def assign(ticket_id: str, agent_id: Optional[str]) -> Dict[str, Any]:
    db = platform_db()
    name = None
    if agent_id:
        agent = await db.platform_admins.find_one({"_id": oid(agent_id)})
        if not agent:
            raise NotFound("Agent not found")
        name = agent.get("full_name")
    result = await db.support_tickets.find_one_and_update(
        {"_id": oid(ticket_id)},
        {"$set": {"assigned_to": agent_id, "assigned_to_name": name,
                  "status": TicketStatus.IN_PROGRESS.value, "updated_at": utcnow()}},
        return_document=True)
    if not result:
        raise NotFound("Ticket not found")
    return serialize(result)

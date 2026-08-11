"""
Support tickets. [INFERRED from support/HelpSupport.tsx + admin/Helpdesk.tsx]

Tickets are stored in the PLATFORM database with a tenant_id, not in the tenant
database. A super admin needs one queue across every organization; with
database-per-tenant, a tenant-local collection would force a fan-out query over
every organization database on every helpdesk page load.

When a partner or client (or any workspace user) raises a ticket from Help &
Support, we also email the super admin(s). Reply-To is the requester's signup
email so the admin can respond directly.
"""
from typing import Any, Dict, List, Optional

from app.core.enums import TicketActor, TicketPriority, TicketStatus
from app.core.exceptions import Forbidden, NotFound
from app.core.utils import build_reference, oid, serialize, utcnow
from app.db.mongo import platform_db
from app.modules.admin.settings import get_setting
from app.schemas.common import PageParams
from app.services.email import send_email, send_support_request_email
from app.services.pagination import paginate


async def _next_reference() -> str:
    doc = await platform_db().counters.find_one_and_update(
        {"name": "ticket"}, {"$inc": {"value": 1}}, upsert=True, return_document=True)
    return build_reference("TKT", 1000 + doc.get("value", 1))


async def _super_admin_emails() -> List[str]:
    """Recipients for Help & Support emails.

    Prefer live platform admin accounts (super admin). If an explicit
    ``support_email`` override is stored in platform settings, include that
    too. Fall back to the default setting only when no admins exist yet.
    """
    emails: set[str] = set()
    async for admin in platform_db().platform_admins.find(
        {"status": {"$ne": "suspended"}}, {"email": 1}
    ):
        if admin.get("email"):
            emails.add(admin["email"].lower())

    override = await platform_db().platform_settings.find_one({"key": "support_email"})
    if override and isinstance(override.get("value"), str) and override["value"].strip():
        emails.add(override["value"].strip().lower())
    elif not emails:
        configured = await get_setting("support_email")
        if isinstance(configured, str) and configured.strip():
            emails.add(configured.strip().lower())
    return sorted(emails)


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

    # Email super admin from the requester's signup address (via Reply-To).
    recipients = await _super_admin_emails()
    if recipients:
        await send_support_request_email(
            to=recipients,
            requester_email=user.email,
            requester_name=user.raw.get("full_name"),
            requester_role=user.role.value,
            reference=reference,
            subject=data.subject,
            message=data.message,
            category=data.category.value,
            priority=data.priority.value,
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

    if admin and ticket.get("requester_email"):
        await send_email(
            to=ticket["requester_email"],
            subject=f"Re: [{ticket['reference']}] {ticket['subject']}",
            html=f"<p>{body}</p><p style='color:#98a2a6'>Reply to this ticket in WebImove "
                 f"under Help &amp; Support.</p>")
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

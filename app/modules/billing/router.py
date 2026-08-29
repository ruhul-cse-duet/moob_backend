"""
client/ClientBilling.tsx  [INFERRED]

This is the consultant billing THEIR CLIENT for immigration services — distinct
from /subscription, which is the consultant paying WebImove. Different money,
different visibility: a client sees only their own invoices and never the
organization's subscription.
"""
from datetime import timedelta
from typing import List, Optional

from fastapi import APIRouter, Depends, Query, status as http
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, Field

from app.core.deps import (
    CurrentUser,
    get_current_user,
    get_tenant_db,
    page_params,
    require_active_tenant,
    require_consultant,
)
from app.core.enums import InvoiceStatus, NotificationType, Role
from app.core.exceptions import BadRequest, Forbidden, NotFound
from app.core.utils import build_reference, oid, serialize, utcnow
from app.db.indexes import next_sequence
from app.schemas.common import PageParams
from app.services.events import notify
from app.services.pagination import paginate

router = APIRouter(prefix="/invoices", tags=["Client Billing"],
                   dependencies=[Depends(require_active_tenant)])


class LineItem(BaseModel):
    description: str
    quantity: float = 1
    unit_price: float
    @property
    def total(self) -> float:
        return round(self.quantity * self.unit_price, 2)


class InvoiceCreate(BaseModel):
    client_id: str
    case_id: Optional[str] = None
    items: List[LineItem] = Field(min_length=1)
    currency: str = "USD"
    tax_rate: float = Field(0.0, ge=0, le=1)
    due_in_days: int = Field(14, ge=1, le=180)
    notes: Optional[str] = None


def _totals(items: List[LineItem], tax_rate: float) -> dict:
    subtotal = round(sum(i.quantity * i.unit_price for i in items), 2)
    tax = round(subtotal * tax_rate, 2)
    return {"subtotal": subtotal, "tax": tax, "total": round(subtotal + tax, 2)}


@router.post("", status_code=http.HTTP_201_CREATED, summary="Issue an invoice to a client")
async def create_invoice(payload: InvoiceCreate,
                         user: CurrentUser = Depends(require_consultant),
                         db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    client = await db.users.find_one({"_id": oid(payload.client_id),
                                      "role": Role.CLIENT.value})
    if not client:
        raise NotFound("Client not found")

    seq = await next_sequence(db, "invoice", start=1000)
    now = utcnow()
    doc = {
        "reference": build_reference("INV", seq),
        "client_id": payload.client_id,
        "client_name": client.get("full_name"),
        "client_email": client.get("email"),
        "case_id": payload.case_id,
        "consultant_id": client.get("consultant_id") or user.id,
        "items": [{**i.model_dump(), "total": round(i.quantity * i.unit_price, 2)}
                  for i in payload.items],
        "currency": payload.currency,
        "tax_rate": payload.tax_rate,
        **_totals(payload.items, payload.tax_rate),
        "status": InvoiceStatus.DRAFT.value,
        "notes": payload.notes,
        "issued_by": user.id,
        "due_at": now + timedelta(days=payload.due_in_days),
        "paid_at": None,
        "created_at": now,
        "updated_at": now,
    }
    invoice_id = str((await db.invoices.insert_one(doc)).inserted_id)
    return serialize({**doc, "_id": oid(invoice_id)})


@router.get("", summary="Invoices (clients see only their own)")
async def list_invoices(status: Optional[InvoiceStatus] = Query(None),
                        client_id: Optional[str] = Query(None),
                        params: PageParams = Depends(page_params),
                        user: CurrentUser = Depends(get_current_user),
                        db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    if user.role == Role.PARTNER:
        raise Forbidden("Partners do not see client billing")
    query = {"client_id": user.id} if user.role == Role.CLIENT else {}
    if client_id and user.role != Role.CLIENT:
        query["client_id"] = client_id
    if status:
        query["status"] = status.value
    return await paginate(db, "invoices", query, params, sort=[("created_at", -1)])


@router.get("/summary", summary="Outstanding / paid / overdue totals")
async def summary(user: CurrentUser = Depends(get_current_user),
                  db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    base = {"client_id": user.id} if user.role == Role.CLIENT else {}
    out = {}
    for st in InvoiceStatus:
        cursor = db.invoices.aggregate([
            {"$match": {**base, "status": st.value}},
            {"$group": {"_id": None, "total": {"$sum": "$total"}, "n": {"$sum": 1}}},
        ])
        row = None
        async for r in cursor:
            row = r
        out[st.value] = {"amount": round((row or {}).get("total", 0) or 0, 2),
                         "count": (row or {}).get("n", 0)}
    out["overdue_now"] = await db.invoices.count_documents(
        {**base, "status": InvoiceStatus.SENT.value, "due_at": {"$lt": utcnow()}})
    return out


@router.get("/{invoice_id}")
async def get_invoice(invoice_id: str,
                      user: CurrentUser = Depends(get_current_user),
                      db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    doc = await db.invoices.find_one({"_id": oid(invoice_id)})
    if not doc:
        raise NotFound("Invoice not found")
    if user.role == Role.CLIENT and doc["client_id"] != user.id:
        raise Forbidden("This invoice is not yours")
    return serialize(doc)


@router.post("/{invoice_id}/send", summary="Send a draft invoice to the client")
async def send_invoice(invoice_id: str,
                       user: CurrentUser = Depends(require_consultant),
                       db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    doc = await db.invoices.find_one_and_update(
        {"_id": oid(invoice_id), "status": InvoiceStatus.DRAFT.value},
        {"$set": {"status": InvoiceStatus.SENT.value, "sent_at": utcnow(),
                  "updated_at": utcnow()}},
        return_document=True)
    if not doc:
        raise BadRequest("Only draft invoices can be sent")
    await notify(db, user_ids=[doc["client_id"]], type=NotificationType.SUBSCRIPTION,
                 title_key="notify.invoice_sent",
                 params={"reference": doc["reference"],
                         "currency": doc["currency"],
                         "amount": f"{doc['total']:.2f}"},
                 body="Due " + doc["due_at"].strftime("%d %b %Y") if doc.get("due_at") else "",
                 data={"invoice_id": invoice_id})
    return serialize(doc)


@router.post("/{invoice_id}/mark-paid", summary="Record payment against an invoice")
async def mark_paid(invoice_id: str, method: Optional[str] = Query(None),
                    user: CurrentUser = Depends(require_consultant),
                    db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    doc = await db.invoices.find_one_and_update(
        {"_id": oid(invoice_id), "status": {"$in": [InvoiceStatus.SENT.value,
                                                    InvoiceStatus.OVERDUE.value]}},
        {"$set": {"status": InvoiceStatus.PAID.value, "paid_at": utcnow(),
                  "payment_method": method, "updated_at": utcnow()}},
        return_document=True)
    if not doc:
        raise BadRequest("Only sent or overdue invoices can be marked paid")
    return serialize(doc)


@router.post("/{invoice_id}/void", summary="Void an invoice")
async def void_invoice(invoice_id: str,
                       user: CurrentUser = Depends(require_consultant),
                       db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    doc = await db.invoices.find_one_and_update(
        {"_id": oid(invoice_id), "status": {"$ne": InvoiceStatus.PAID.value}},
        {"$set": {"status": InvoiceStatus.VOID.value, "updated_at": utcnow()}},
        return_document=True)
    if not doc:
        raise BadRequest("Paid invoices cannot be voided")
    return serialize(doc)

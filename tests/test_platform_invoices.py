"""
Super Admin · Subscriptions & Billing, Invoices tab.

Two properties matter more than the rest, because both failure modes involve
real money:

* **Refund Stripe first, mark our row second.** The other order produces an
  invoice that reads "refunded" while the customer's statement says otherwise.
* **A closed invoice cannot be resolved twice.** Refunding a refunded invoice
  would take the money back a second time.

The rest pins the tab itself: the header counters, and that settling an invoice
does not silently reopen a workspace.
"""
import pytest
from bson import ObjectId
from mongomock_motor import AsyncMongoMockClient

from app.core.enums import BillingCycle, PlanCode, PlatformInvoiceStatus
from app.core.exceptions import BadRequest, Conflict, NotFound
from app.modules.admin import billing
from app.services import audit
from app.services import invoices as invoice_service

TENANT_ID = ObjectId()


class Admin:
    id = str(ObjectId())
    email = "superadmin@webimove.com"


@pytest.fixture
def db(monkeypatch):
    pdb = AsyncMongoMockClient()["webimove_platform"]
    for module in (billing, invoice_service, audit):
        monkeypatch.setattr(module, "platform_db", lambda: pdb)
    return pdb


@pytest.fixture
async def seeded(db):
    await db.tenants.insert_one({
        "_id": TENANT_ID, "name": "Maple Route Immigration",
        "owner_email": "ops@maple.example", "status": "active",
        "plan_code": "professional",
    })
    return db


async def make(db, status=PlatformInvoiceStatus.PAID, amount=129.0, **extra):
    doc = {
        "reference": await invoice_service.next_reference(),
        "tenant_id": str(TENANT_ID),
        "organization_name": "Maple Route Immigration",
        "amount": amount, "currency": "usd", "status": status.value,
        "billing_cycle": "monthly", "payment_method": "Visa •••• 1123",
        "stripe_payment_intent_id": "pi_test_123",
        "created_at": __import__("app.core.utils", fromlist=["utcnow"]).utcnow(),
    }
    doc.update(extra)
    doc["_id"] = (await db.platform_invoices.insert_one(doc)).inserted_id
    return doc


# ------------------------------------------------------------------ the tab
class TestInvoiceList:
    @pytest.mark.asyncio
    async def test_the_header_counters_cover_every_invoice_not_the_page(self, seeded):
        """Paging must not change the headline numbers."""
        await make(seeded, PlatformInvoiceStatus.PAID)
        await make(seeded, PlatformInvoiceStatus.PAID)
        await make(seeded, PlatformInvoiceStatus.FAILED)
        await make(seeded, PlatformInvoiceStatus.PENDING)

        from app.schemas.common import PageParams
        page = await billing.list_invoices(
            status=None, tenant_id=None, params=PageParams(page=1, page_size=1),
            user=Admin())

        assert len(page["items"]) == 1
        assert page["summary"]["failed_payments"] == 1
        assert page["summary"]["pending_payments"] == 1
        # Both paid invoices belong to one organization.
        assert page["summary"]["paying_organizations"] == 1

    @pytest.mark.asyncio
    async def test_filtering_by_status(self, seeded):
        await make(seeded, PlatformInvoiceStatus.PAID)
        await make(seeded, PlatformInvoiceStatus.FAILED)

        from app.schemas.common import PageParams
        page = await billing.list_invoices(
            status=PlatformInvoiceStatus.FAILED, tenant_id=None,
            params=PageParams(page=1, page_size=20), user=Admin())

        assert len(page["items"]) == 1
        assert page["items"][0]["status"] == "failed"

    @pytest.mark.asyncio
    async def test_a_missing_invoice_is_a_404(self, seeded):
        with pytest.raises(NotFound):
            await billing.get_invoice(str(ObjectId()), user=Admin())


# ---------------------------------------------------------------- mark paid
class TestMarkPaid:
    @pytest.mark.asyncio
    async def test_a_failed_invoice_can_be_settled(self, seeded):
        invoice = await make(seeded, PlatformInvoiceStatus.FAILED)

        result = await billing.mark_invoice_paid(
            str(invoice["_id"]), note="paid by bank transfer", user=Admin())

        assert result["status"] == "paid"
        stored = await seeded.platform_invoices.find_one({"_id": invoice["_id"]})
        assert stored["paid_at"] is not None

    @pytest.mark.asyncio
    async def test_settling_does_not_reopen_the_workspace(self, seeded):
        """Two decisions, not one: the customer may owe more than this invoice,
        so reactivation stays an explicit separate call."""
        await seeded.tenants.update_one({"_id": TENANT_ID},
                                        {"$set": {"status": "past_due"}})
        invoice = await make(seeded, PlatformInvoiceStatus.FAILED)

        await billing.mark_invoice_paid(str(invoice["_id"]), note=None, user=Admin())

        tenant = await seeded.tenants.find_one({"_id": TENANT_ID})
        assert tenant["status"] == "past_due"

    @pytest.mark.asyncio
    async def test_an_already_paid_invoice_cannot_be_paid_again(self, seeded):
        invoice = await make(seeded, PlatformInvoiceStatus.PAID)

        with pytest.raises(Conflict):
            await billing.mark_invoice_paid(str(invoice["_id"]), note=None, user=Admin())


# ---------------------------------------------------------------- write off
class TestWriteOff:
    @pytest.mark.asyncio
    async def test_a_failed_invoice_can_be_written_off_with_a_reason(self, seeded):
        invoice = await make(seeded, PlatformInvoiceStatus.FAILED)

        result = await billing.write_off_invoice(
            str(invoice["_id"]), reason="Customer closed; not pursuing", user=Admin())

        assert result["status"] == "written_off"
        assert result["write_off_reason"] == "Customer closed; not pursuing"

    @pytest.mark.asyncio
    async def test_a_refunded_invoice_cannot_be_written_off(self, seeded):
        invoice = await make(seeded, PlatformInvoiceStatus.REFUNDED)

        with pytest.raises(Conflict):
            await billing.write_off_invoice(
                str(invoice["_id"]), reason="whatever", user=Admin())


# ------------------------------------------------------------------- refund
class TestRefund:
    @pytest.mark.asyncio
    async def test_a_paid_invoice_is_refunded_at_stripe_then_recorded(
            self, seeded, monkeypatch):
        calls = {}

        async def fake_refund(**kwargs):
            calls.update(kwargs)
            return {"success": True, "refund_id": "re_test_1", "message": "ok"}

        monkeypatch.setattr(billing.stripe_service, "refund_invoice", fake_refund)
        invoice = await make(seeded, PlatformInvoiceStatus.PAID, amount=129.0)

        result = await billing.refund_invoice(
            str(invoice["_id"]), amount=None, reason=None, user=Admin())

        assert calls["payment_intent_id"] == "pi_test_123"
        assert result["status"] == "refunded"
        assert result["stripe_refund_id"] == "re_test_1"
        assert result["refunded_amount"] == 129.0

    @pytest.mark.asyncio
    async def test_a_failed_stripe_refund_leaves_the_invoice_paid(
            self, seeded, monkeypatch):
        """The failure the customer would notice on their statement: our row
        saying refunded while Stripe never returned the money."""
        async def fake_refund(**kwargs):
            return {"success": False, "refund_id": None,
                    "message": "charge has already been refunded"}

        monkeypatch.setattr(billing.stripe_service, "refund_invoice", fake_refund)
        invoice = await make(seeded, PlatformInvoiceStatus.PAID)

        with pytest.raises(BadRequest):
            await billing.refund_invoice(
                str(invoice["_id"]), amount=None, reason=None, user=Admin())

        stored = await seeded.platform_invoices.find_one({"_id": invoice["_id"]})
        assert stored["status"] == "paid"

    @pytest.mark.asyncio
    async def test_only_a_paid_invoice_can_be_refunded(self, seeded):
        invoice = await make(seeded, PlatformInvoiceStatus.FAILED)

        with pytest.raises(Conflict):
            await billing.refund_invoice(
                str(invoice["_id"]), amount=None, reason=None, user=Admin())

    @pytest.mark.asyncio
    async def test_refunding_more_than_the_invoice_is_refused(self, seeded):
        invoice = await make(seeded, PlatformInvoiceStatus.PAID, amount=129.0)

        with pytest.raises(BadRequest):
            await billing.refund_invoice(
                str(invoice["_id"]), amount=500.0, reason=None, user=Admin())


# ------------------------------------------------------- offline / manual
class TestManualInvoice:
    @pytest.mark.asyncio
    async def test_a_bank_transfer_can_be_recorded(self, seeded):
        """Seoul Bridge Consulting in the mockup: pending, bank transfer, no
        Stripe involvement at all."""
        result = await billing.create_manual_invoice(
            billing.ManualInvoice(
                tenant_id=str(TENANT_ID), amount=129.0,
                billing_cycle=BillingCycle.MONTHLY, plan_code=PlanCode.PROFESSIONAL,
                payment_method="Bank transfer",
                status=PlatformInvoiceStatus.PENDING),
            user=Admin())

        assert result["status"] == "pending"
        assert result["payment_method"] == "Bank transfer"
        assert result["reference"].startswith("INV-")
        # No Stripe ids: a refund here has nothing to call, so it must be a
        # write-off instead.
        assert result.get("stripe_payment_intent_id") is None

    @pytest.mark.asyncio
    async def test_an_unknown_organization_is_refused(self, seeded):
        with pytest.raises(NotFound):
            await billing.create_manual_invoice(
                billing.ManualInvoice(tenant_id=str(ObjectId()), amount=10.0),
                user=Admin())


# ------------------------------------------------------- webhook recording
class TestRecordingFromStripe:
    @pytest.mark.asyncio
    async def test_a_paid_event_becomes_an_invoice(self, seeded):
        tenant = await seeded.tenants.find_one({"_id": TENANT_ID})

        row = await invoice_service.record_from_stripe(
            tenant=tenant, obj={"id": "in_1", "amount_paid": 12900,
                                "currency": "usd", "payment_intent": "pi_1"},
            status=PlatformInvoiceStatus.PAID)

        assert row["amount"] == 129.0
        assert row["organization_name"] == "Maple Route Immigration"

    @pytest.mark.asyncio
    async def test_the_same_stripe_event_twice_is_one_invoice(self, seeded):
        """Stripe retries any non-2xx and can deliver the same event twice; a
        duplicate would show the organization as having paid twice."""
        tenant = await seeded.tenants.find_one({"_id": TENANT_ID})
        obj = {"id": "in_dup", "amount_paid": 4900, "currency": "usd"}

        await invoice_service.record_from_stripe(
            tenant=tenant, obj=obj, status=PlatformInvoiceStatus.PAID)
        await invoice_service.record_from_stripe(
            tenant=tenant, obj=obj, status=PlatformInvoiceStatus.PAID)

        assert await seeded.platform_invoices.count_documents({"stripe_invoice_id": "in_dup"}) == 1

    @pytest.mark.asyncio
    async def test_a_failed_invoice_that_later_clears_keeps_its_reference(self, seeded):
        """The reference is what a customer quotes; it must not change when the
        retry succeeds."""
        tenant = await seeded.tenants.find_one({"_id": TENANT_ID})
        obj = {"id": "in_retry", "amount_due": 12900, "currency": "usd"}

        failed = await invoice_service.record_from_stripe(
            tenant=tenant, obj=obj, status=PlatformInvoiceStatus.FAILED)
        paid = await invoice_service.record_from_stripe(
            tenant=tenant, obj=obj, status=PlatformInvoiceStatus.PAID)

        assert paid["reference"] == failed["reference"]
        assert paid["status"] == "paid"

    def test_the_card_is_described_for_the_list(self):
        """"Visa •••• 4417" — Stripe has moved this field between API versions,
        so each known location is tried."""
        assert invoice_service.describe_card(
            {"payment_method_details": {"card": {"brand": "visa", "last4": "4417"}}}
        ) == "Visa •••• 4417"
        assert invoice_service.describe_card(
            {"charges": {"data": [{"payment_method_details":
                                   {"card": {"brand": "mastercard", "last4": "8802"}}}]}}
        ) == "Mastercard •••• 8802"
        assert invoice_service.describe_card({}) is None

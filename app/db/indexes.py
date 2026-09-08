"""Index definitions, created concurrently.

Every `create_index` is a round trip to Atlas. Awaited one after another the 35
platform indexes cost 35 round trips before the API answers its first request -
several seconds on a free cluster, paid on every boot and every deploy. They do
not depend on each other, so they are gathered instead: the cost becomes roughly
one round trip rather than 35.

`create_index` is idempotent, which is what makes running this on every boot
safe. It is also why the saving is worth having - after the first deploy the
calls have nothing to do, and the only thing left to pay for is the latency.
"""
import asyncio
import logging
from typing import Any, Coroutine, List

from motor.motor_asyncio import AsyncIOMotorDatabase

from app.db.mongo import platform_db

logger = logging.getLogger("app.db.indexes")


async def _gather(tasks: List[Coroutine[Any, Any, Any]], label: str) -> None:
    """Runs every index creation at once and reports any that failed.

    `return_exceptions` matters: one index failing - a stale unique constraint
    over data that now has duplicates - must not abandon the other 34. The API
    boots either way; a missing index is slow, not broken.
    """
    results = await asyncio.gather(*tasks, return_exceptions=True)
    failures = [r for r in results if isinstance(r, BaseException)]
    if not failures:
        return

    logger.warning("%s: %d of %d indexes could not be created; first was %s",
                   label, len(failures), len(results), failures[0])

    # Every single one failing is not a stale constraint - it is the database
    # being unreachable, and swallowing that reported "indexes ready" over a
    # connection that did not exist. The caller decides what to do about it;
    # startup boots degraded and says which settings to check, which is the
    # message that was being hidden.
    if len(failures) == len(results):
        raise failures[0]


async def ensure_platform_indexes() -> None:
    db = platform_db()
    await _gather([
        db.tenants.create_index("slug", unique=True),
        db.tenants.create_index("owner_email"),
        db.tenants.create_index("status"),
        # Global directory: one email == one account, and it tells us which tenant DB to open.
        db.user_directory.create_index("email", unique=True),
        db.user_directory.create_index("tenant_id"),
        db.signups.create_index("email"),
        db.signups.create_index("created_at", expireAfterSeconds=60 * 60 * 24 * 7),
        db.otp_codes.create_index([("email", 1), ("purpose", 1)]),
        db.otp_codes.create_index("expires_at", expireAfterSeconds=0),
        # Brute-force counters. The TTL is what keeps this collection from growing
        # without bound; the document carries its own expiry.
        db.auth_throttle.create_index("expires_at", expireAfterSeconds=0),
        db.auth_throttle.create_index("locked_until", sparse=True),

        db.refresh_tokens.create_index("token", unique=True),
        db.refresh_tokens.create_index("expires_at", expireAfterSeconds=0),
        db.subscriptions.create_index("tenant_id"),
        # Stripe delivers the same event more than once; the unique _id is what makes
        # the webhook idempotent. Kept 90 days - long enough to outlive Stripe's retries.
        db.stripe_events.create_index("received_at", expireAfterSeconds=60 * 60 * 24 * 90),
        db.tenants.create_index("stripe_customer_id", sparse=True),
        db.tenants.create_index("stripe_subscription_id", sparse=True),
        db.plans.create_index("code", unique=True),
        db.platform_admins.create_index("email", unique=True),
        db.audit_log.create_index([("tenant_id", 1), ("created_at", -1)]),
        db.audit_log.create_index([("action", 1), ("created_at", -1)]),
        db.audit_log.create_index("actor_id"),

        # Helpdesk lives platform-side so one queue spans every tenant.
        db.support_tickets.create_index([("status", 1), ("created_at", -1)]),
        db.support_tickets.create_index([("tenant_id", 1), ("created_at", -1)]),
        db.support_tickets.create_index("reference", unique=True),
        db.support_tickets.create_index("assigned_to"),
        db.ticket_messages.create_index([("ticket_id", 1), ("created_at", 1)]),

        # Unique: acceptance is recorded against a version *string*, so two
        # rows sharing (kind, version) make "this user accepted Privacy
        # Policy 2.0" ambiguous about which 2.0 they actually saw.
        # Platform invoices: the Invoices tab lists newest first and filters by
        # status, and the Stripe id is what makes webhook delivery idempotent -
        # the same invoice.paid event arriving twice must not bill twice.
        db.platform_invoices.create_index([("created_at", -1)]),
        db.platform_invoices.create_index([("status", 1), ("created_at", -1)]),
        db.platform_invoices.create_index([("tenant_id", 1), ("created_at", -1)]),
        db.platform_invoices.create_index("reference", unique=True),
        db.platform_invoices.create_index("stripe_invoice_id", unique=True, sparse=True),

        db.policies.create_index([("kind", 1), ("version", -1)], unique=True),
        db.policy_acceptances.create_index([("user_email", 1), ("kind", 1)]),
        db.platform_settings.create_index("key", unique=True),
        db.counters.create_index("name", unique=True),

        # Push notifications. Device tokens live platform-side even for tenant
        # users: a notification names user ids, and resolving them to devices must
        # not depend on knowing which tenant database each one came from.
        #
        # The token is the identity, hence unique - the same handset re-registering
        # updates its row rather than growing a second one, and a phone handed to a
        # new person moves across instead of pushing to both accounts.
        db.device_tokens.create_index("token", unique=True),
        db.device_tokens.create_index("user_id"),
        db.device_tokens.create_index([("user_id", 1), ("active", 1)]),
        db.push_preferences.create_index("user_id", unique=True),
    ], "platform")


async def ensure_tenant_indexes(db: AsyncIOMotorDatabase) -> None:
    await _gather([
        db.users.create_index("email", unique=True),
        db.users.create_index("role"),
        db.users.create_index("status"),
        # Ownership: a client has one consultant; a partner may serve several.
        db.users.create_index([("role", 1), ("consultant_id", 1)]),
        db.users.create_index([("role", 1), ("consultant_ids", 1)]),
        db.counters.create_index("name", unique=True),

        db.requests.create_index("reference", unique=True),
        db.requests.create_index([("status", 1), ("created_at", -1)]),
        db.requests.create_index("client_id"),
        db.requests.create_index("consultant_id"),

        db.documents.create_index([("request_id", 1), ("status", 1)]),
        db.documents.create_index("case_id"),
        db.documents.create_index("client_id"),
        db.documents.create_index([("consultant_id", 1), ("status", 1)]),

        db.cases.create_index("reference", unique=True),
        db.cases.create_index([("stage", 1), ("deadline", 1)]),
        db.cases.create_index("client_id"),
        db.cases.create_index("consultant_id"),

        db.tasks.create_index([("assignee_id", 1), ("status", 1)]),
        db.tasks.create_index("case_id"),
        db.tasks.create_index([("consultant_id", 1), ("status", 1)]),

        db.notifications.create_index([("user_id", 1), ("read", 1), ("created_at", -1)]),
        db.activities.create_index([("created_at", -1)]),
        db.messages.create_index([("thread_id", 1), ("created_at", 1)]),
        db.threads.create_index("participant_ids"),
        db.ai_conversations.create_index([("user_id", 1), ("updated_at", -1)]),
        db.consents.create_index([("user_id", 1), ("type", 1)], unique=True),
        db.data_requests.create_index([("user_id", 1), ("created_at", -1)]),
        db.earnings.create_index([("partner_id", 1), ("status", 1)]),
        db.earnings.create_index("task_id"),
        db.earnings.create_index([("consultant_id", 1), ("status", 1)]),
        db.invoices.create_index([("client_id", 1), ("status", 1)]),
        db.invoices.create_index("reference", unique=True),
        db.invoices.create_index([("consultant_id", 1), ("status", 1)]),
        db.login_history.create_index([("user_id", 1), ("created_at", -1)]),
        db.appointments.create_index([("consultant_id", 1), ("starts_at", 1)]),

        # GridFS buckets: fs.files/fs.chunks indexes are created by the driver on first
        # write; these are for looking a blob back up from its owning record.
        db["documents.files"].create_index("metadata.document_id"),
        db["deliverables.files"].create_index("metadata.task_id"),
    ], "tenant")


async def next_sequence(db: AsyncIOMotorDatabase, name: str, start: int = 100) -> int:
    doc = await db.counters.find_one_and_update(
        {"name": name},
        {"$inc": {"value": 1}},
        upsert=True,
        return_document=True,
    )
    value = doc.get("value", 1)
    return start + value

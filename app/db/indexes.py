from motor.motor_asyncio import AsyncIOMotorDatabase

from app.db.mongo import platform_db


async def ensure_platform_indexes() -> None:
    db = platform_db()
    await db.tenants.create_index("slug", unique=True)
    await db.tenants.create_index("owner_email")
    await db.tenants.create_index("status")
    # Global directory: one email == one account, and it tells us which tenant DB to open.
    await db.user_directory.create_index("email", unique=True)
    await db.user_directory.create_index("tenant_id")
    await db.signups.create_index("email")
    await db.signups.create_index("created_at", expireAfterSeconds=60 * 60 * 24 * 7)
    await db.otp_codes.create_index([("email", 1), ("purpose", 1)])
    await db.otp_codes.create_index("expires_at", expireAfterSeconds=0)
    # Brute-force counters. The TTL is what keeps this collection from growing
    # without bound; the document carries its own expiry.
    await db.auth_throttle.create_index("expires_at", expireAfterSeconds=0)
    await db.auth_throttle.create_index("locked_until", sparse=True)

    await db.refresh_tokens.create_index("token", unique=True)
    await db.refresh_tokens.create_index("expires_at", expireAfterSeconds=0)
    await db.subscriptions.create_index("tenant_id")
    # Stripe delivers the same event more than once; the unique _id is what makes
    # the webhook idempotent. Kept 90 days - long enough to outlive Stripe's retries.
    await db.stripe_events.create_index("received_at", expireAfterSeconds=60 * 60 * 24 * 90)
    await db.tenants.create_index("stripe_customer_id", sparse=True)
    await db.tenants.create_index("stripe_subscription_id", sparse=True)
    await db.plans.create_index("code", unique=True)
    await db.platform_admins.create_index("email", unique=True)
    await db.audit_log.create_index([("tenant_id", 1), ("created_at", -1)])
    await db.audit_log.create_index([("action", 1), ("created_at", -1)])
    await db.audit_log.create_index("actor_id")

    # Helpdesk lives platform-side so one queue spans every tenant.
    await db.support_tickets.create_index([("status", 1), ("created_at", -1)])
    await db.support_tickets.create_index([("tenant_id", 1), ("created_at", -1)])
    await db.support_tickets.create_index("reference", unique=True)
    await db.support_tickets.create_index("assigned_to")
    await db.ticket_messages.create_index([("ticket_id", 1), ("created_at", 1)])

    await db.policies.create_index([("kind", 1), ("version", -1)])
    await db.policy_acceptances.create_index([("user_email", 1), ("kind", 1)])
    await db.platform_settings.create_index("key", unique=True)
    await db.counters.create_index("name", unique=True)

    # Push notifications. Device tokens live platform-side even for tenant
    # users: a notification names user ids, and resolving them to devices must
    # not depend on knowing which tenant database each one came from.
    #
    # The token is the identity, hence unique - the same handset re-registering
    # updates its row rather than growing a second one, and a phone handed to a
    # new person moves across instead of pushing to both accounts.
    await db.device_tokens.create_index("token", unique=True)
    await db.device_tokens.create_index("user_id")
    await db.device_tokens.create_index([("user_id", 1), ("active", 1)])
    await db.push_preferences.create_index("user_id", unique=True)


async def ensure_tenant_indexes(db: AsyncIOMotorDatabase) -> None:
    await db.users.create_index("email", unique=True)
    await db.users.create_index("role")
    await db.users.create_index("status")
    # Ownership: a client has one consultant; a partner may serve several.
    await db.users.create_index([("role", 1), ("consultant_id", 1)])
    await db.users.create_index([("role", 1), ("consultant_ids", 1)])
    await db.counters.create_index("name", unique=True)

    await db.requests.create_index("reference", unique=True)
    await db.requests.create_index([("status", 1), ("created_at", -1)])
    await db.requests.create_index("client_id")
    await db.requests.create_index("consultant_id")

    await db.documents.create_index([("request_id", 1), ("status", 1)])
    await db.documents.create_index("case_id")
    await db.documents.create_index("client_id")
    await db.documents.create_index([("consultant_id", 1), ("status", 1)])

    await db.cases.create_index("reference", unique=True)
    await db.cases.create_index([("stage", 1), ("deadline", 1)])
    await db.cases.create_index("client_id")
    await db.cases.create_index("consultant_id")

    await db.tasks.create_index([("assignee_id", 1), ("status", 1)])
    await db.tasks.create_index("case_id")
    await db.tasks.create_index([("consultant_id", 1), ("status", 1)])

    await db.notifications.create_index([("user_id", 1), ("read", 1), ("created_at", -1)])
    await db.activities.create_index([("created_at", -1)])
    await db.messages.create_index([("thread_id", 1), ("created_at", 1)])
    await db.threads.create_index("participant_ids")
    await db.ai_conversations.create_index([("user_id", 1), ("updated_at", -1)])
    await db.consents.create_index([("user_id", 1), ("type", 1)], unique=True)
    await db.data_requests.create_index([("user_id", 1), ("created_at", -1)])
    await db.earnings.create_index([("partner_id", 1), ("status", 1)])
    await db.earnings.create_index("task_id")
    await db.earnings.create_index([("consultant_id", 1), ("status", 1)])
    await db.invoices.create_index([("client_id", 1), ("status", 1)])
    await db.invoices.create_index("reference", unique=True)
    await db.invoices.create_index([("consultant_id", 1), ("status", 1)])
    await db.login_history.create_index([("user_id", 1), ("created_at", -1)])
    await db.appointments.create_index([("consultant_id", 1), ("starts_at", 1)])

    # GridFS buckets: fs.files/fs.chunks indexes are created by the driver on first
    # write; these are for looking a blob back up from its owning record.
    await db["documents.files"].create_index("metadata.document_id")
    await db["deliverables.files"].create_index("metadata.task_id")


async def next_sequence(db: AsyncIOMotorDatabase, name: str, start: int = 100) -> int:
    doc = await db.counters.find_one_and_update(
        {"name": name},
        {"$inc": {"value": 1}},
        upsert=True,
        return_document=True,
    )
    value = doc.get("value", 1)
    return start + value

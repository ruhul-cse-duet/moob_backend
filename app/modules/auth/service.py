import logging
from datetime import timedelta
from typing import Any, Dict, Optional

from app.core.enums import (
    AuditAction,
    BillingCycle,
    LOGIN_PORTAL_ROLE_MAP,
    LoginPortalRole,
    NotificationType,
    OtpPurpose,
    PlanCode,
    RequestStatus,
    Role,
    TenantStatus,
    UserStatus,
)
from app.core.exceptions import BadRequest, Conflict, NotFound, Unauthorized
from app.core.security import (
    create_access_token,
    create_onboarding_token,
    create_refresh_token,
    decode_token,
    hash_password,
    verify_password,
)
from app.core.config import settings
from app.core.utils import (
    build_reference,
    oid,
    random_token,
    serialize,
    slugify_db,
    utcnow,
)
from app.db.indexes import ensure_tenant_indexes, next_sequence
from app.db.mongo import drop_tenant_db, platform_db, tenant_db
from app.modules.subscriptions.plans import order_summary, plan_by_code
from app.services import audit, throttle
from app.services import invites
from app.services import otp as otp_service
from app.services import stripe_service
from app.services.events import notify
from app.services.ownership import consultant_map, link_partner_to_consultant


logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Signup - 5 steps, state kept in platform.signups until payment succeeds
# --------------------------------------------------------------------------- #
async def start_signup(data) -> Dict[str, Any]:
    db = platform_db()
    email = data.email.lower()

    if await db.user_directory.find_one({"email": email}):
        raise Conflict("An account with this email already exists")

    await db.signups.delete_many({"email": email, "completed": {"$ne": True}})
    doc = {
        "email": email,
        "full_name": data.full_name,
        "mobile": data.mobile,
        "dob": getattr(data, "dob", None),
        "profile_photo_url": getattr(data, "profile_photo_url", None),
        "step": "organization",
        "email_verified": False,
        "completed": False,
        "created_at": utcnow(),
    }
    result = await db.signups.insert_one(doc)
    return {
        "onboarding_token": create_onboarding_token(signup_id=str(result.inserted_id), step="organization"),
        "step": "personal",
        "next_step": "organization",
    }


async def _load_signup(token: str) -> Dict[str, Any]:
    try:
        payload = decode_token(token, "onboarding")
    except ValueError as exc:
        raise Unauthorized(str(exc)) from exc
    signup = await platform_db().signups.find_one({"_id": oid(payload["sub"])})
    if not signup or signup.get("completed"):
        raise Unauthorized("This signup session is no longer valid")
    return signup


async def set_organization(token: str, data) -> Dict[str, Any]:
    db = platform_db()
    signup = await _load_signup(token)

    slug = slugify_db(data.organization_name)
    if await db.tenants.find_one({"slug": slug}):
        slug = f"{slug}_{random_token(4).lower()}"

    await db.signups.update_one(
        {"_id": signup["_id"]},
        {"$set": {
            "organization": {
                "name": data.organization_name,
                "slug": slug,
                "business_type": data.business_type,
                "country": data.country,
                "city": data.city,
                "office_address": data.office_address,
            },
            "step": "verify",
        }},
    )
    issued = await otp_service.issue_otp(email=signup["email"], purpose=OtpPurpose.EMAIL_VERIFICATION)
    return {
        "onboarding_token": create_onboarding_token(signup_id=str(signup["_id"]), step="verify"),
        "step": "organization",
        "next_step": "verify",
        **issued,
    }


async def verify_signup_email(token: str, code: str) -> Dict[str, Any]:
    db = platform_db()
    signup = await _load_signup(token)
    await otp_service.verify_otp(
        email=signup["email"], purpose=OtpPurpose.EMAIL_VERIFICATION, code=code
    )
    await db.signups.update_one(
        {"_id": signup["_id"]}, {"$set": {"email_verified": True, "step": "password"}}
    )
    return {
        "onboarding_token": create_onboarding_token(signup_id=str(signup["_id"]), step="password"),
        "step": "verify",
        "next_step": "password",
    }


async def set_password(token: str, data) -> Dict[str, Any]:
    db = platform_db()
    signup = await _load_signup(token)
    if not signup.get("email_verified"):
        raise BadRequest("Verify your email before creating a password")

    await db.signups.update_one(
        {"_id": signup["_id"]},
        {"$set": {
            "password_hash": hash_password(data.password),
            "step": "plan",
        }}
    )
    return {
        "onboarding_token": create_onboarding_token(signup_id=str(signup["_id"]), step="plan"),
        "step": "password",
        "next_step": "plan",
    }


async def resend_signup_otp(token: str) -> Dict[str, Any]:
    signup = await _load_signup(token)
    return await otp_service.issue_otp(
        email=signup["email"], purpose=OtpPurpose.EMAIL_VERIFICATION
    )


async def choose_plan(token: str, data) -> Dict[str, Any]:
    db = platform_db()
    signup = await _load_signup(token)
    if not signup.get("email_verified"):
        raise BadRequest("Verify your email before choosing a plan")

    summary = await order_summary(data.plan_code, data.billing_cycle)
    renews_on = utcnow() + (
        timedelta(days=365) if data.billing_cycle == BillingCycle.ANNUAL else timedelta(days=30)
    )
    await db.signups.update_one(
        {"_id": signup["_id"]},
        {"$set": {
            "plan": {
                "code": data.plan_code.value,
                "billing_cycle": data.billing_cycle.value,
                **summary,
                "renews_on": renews_on,
            },
            "step": "payment",
        }},
    )
    return {
        "onboarding_token": create_onboarding_token(signup_id=str(signup["_id"]), step="payment"),
        "step": "plan",
        "next_step": "payment",
        "order_summary": {
            "plan_code": data.plan_code,
            "billing_cycle": data.billing_cycle,
            "renews_on": renews_on,
            **summary,
        },
    }


async def complete_payment(token: str, data) -> Dict[str, Any]:
    """Creates the tenant, its dedicated database and the owner account."""
    db = platform_db()
    signup = await _load_signup(token)
    if not signup.get("organization"):
        raise BadRequest("Organization details are missing")
    if not signup.get("email_verified"):
        raise BadRequest("Email is not verified")
    if not signup.get("plan"):
        raise BadRequest("Choose a plan first")

    # Charge once per signup, ever. Provisioning below can fail after the money
    # has moved; without this a retry would take the customer's money twice.
    # `completed` is only set at the very end, so a retry is genuinely reachable.
    reference = signup.get("charge_reference")
    stripe_ids = signup.get("stripe", {})
    if not reference:
        payment = await _take_payment(signup, data)
        if not payment["success"]:
            raise BadRequest(payment["message"])
        reference = payment["reference"]
        stripe_ids = {"customer_id": payment.get("customer_id"),
                      "subscription_id": payment.get("subscription_id")}
        await db.signups.update_one(
            {"_id": signup["_id"]},
            {"$set": {"charge_reference": reference, "charged_at": utcnow(),
                      "stripe": stripe_ids}},
        )

    org = signup["organization"]
    plan = signup["plan"]
    now = utcnow()

    tenant_doc = {
        "name": org["name"],
        "slug": org["slug"],
        "business_type": org["business_type"],
        "country": org["country"],
        "city": org.get("city"),
        "office_address": org["office_address"],
        "owner_email": signup["email"],
        "owner_name": signup["full_name"],
        # Workspace stays locked until a platform admin clicks Approve.
        "status": TenantStatus.AWAITING_APPROVAL.value,
        "verified": False,
        "plan_code": plan["code"],
        "billing_cycle": plan["billing_cycle"],
        "referral_code": random_token(8).upper(),
        "activated_at": None,
        "signed_up_at": now,
        "renews_on": plan["renews_on"],
        # Set only on the subscription path. The webhook needs these to match a
        # renewal or a failed payment back to this organization.
        "stripe_customer_id": stripe_ids.get("customer_id"),
        "stripe_subscription_id": stripe_ids.get("subscription_id"),
        "created_at": now,
        "updated_at": now,
    }
    tenant_id = None

    # Everything from here is provisioning. The card is already charged, so a
    # failure must not leave a half-built workspace behind - the customer would
    # be paying for an organization with no database and no owner account.
    try:
        tenant_id = str((await db.tenants.insert_one(tenant_doc)).inserted_id)

        tdb = tenant_db(tenant_id)
        await ensure_tenant_indexes(tdb)

        owner = {
            "email": signup["email"],
            "full_name": signup["full_name"],
            "mobile": signup["mobile"],
            "password_hash": signup["password_hash"],
            "role": Role.CONSULTANT_OWNER.value,
            "title": "Senior Consultant",
            "status": UserStatus.ACTIVE.value,
            "email_verified": True,
            "language": "EN",
            "avatar_url": None,
            "created_at": now,
            "updated_at": now,
        }
        user_id = str((await tdb.users.insert_one(owner)).inserted_id)

        await db.user_directory.insert_one({
            "email": signup["email"],
            "tenant_id": tenant_id,
            "user_id": user_id,
            "role": Role.CONSULTANT_OWNER.value,
            "created_at": now,
        })
        await db.subscriptions.insert_one({
            "tenant_id": tenant_id,
            "plan_code": plan["code"],
            "billing_cycle": plan["billing_cycle"],
            "amount": plan["subtotal"],
            "tax": plan["estimated_tax"],
            "total": plan["total_due_today"],
            "status": "active",
            "started_at": now,
            "renews_on": plan["renews_on"],
            "payment_reference": reference,
            "card_last4": (data.card_number or "")[-4:] or None,
            "stripe_customer_id": stripe_ids.get("customer_id"),
            "stripe_subscription_id": stripe_ids.get("subscription_id"),
            # A one-off charge has no renewal behind it; a subscription does.
            "recurring": bool(stripe_ids.get("subscription_id")),
            "created_at": now,
        })
        await db.signups.update_one(
            {"_id": signup["_id"]}, {"$set": {"completed": True, "tenant_id": tenant_id}}
        )
        await audit.record(action=AuditAction.TENANT_CREATED, actor_id=user_id,
                           actor_email=signup["email"], actor_role=Role.CONSULTANT_OWNER.value,
                           tenant_id=tenant_id, subject=org["name"],
                           detail=f"{plan['code']} / {plan['billing_cycle']}")
    except Exception:
        logger.exception("Provisioning failed after charge %s - rolling back", reference)
        await _rollback_provisioning(tenant_id)
        # `charge_reference` stays on the signup, so the retry provisions again
        # without charging a second time.
        raise

    return {
        "success": True,
        "message": "Payment received. Your organization is awaiting platform approval.",
        "tenant_id": tenant_id,
        "organization_name": org["name"],
        "plan_code": PlanCode(plan["code"]),
        "renews_on": plan["renews_on"],
        "referral_code": tenant_doc["referral_code"],
        "status": TenantStatus.AWAITING_APPROVAL.value,
        "awaiting_approval": True,
        "workspace_ready": False,
        "access_token": create_access_token(
            user_id=user_id, role=Role.CONSULTANT_OWNER.value,
            tenant_id=tenant_id, email=signup["email"],
        ),
        "refresh_token": await _store_refresh(user_id, tenant_id),
    }


async def _take_payment(signup: Dict[str, Any], data) -> Dict[str, Any]:
    """Charge the customer, preferring a real recurring subscription.

    The subscription path needs both a Stripe.js payment method from the caller
    and a Price mapped for this plan. When either is missing we fall back to the
    legacy one-off charge - which works, but nothing renews it, so the operator
    should map STRIPE_PRICES and move the frontend to Stripe.js.
    """
    plan = signup["plan"]
    plan_code = PlanCode(plan["code"])
    billing_cycle = BillingCycle(plan["billing_cycle"])
    idempotency_key = f"signup_{signup['_id']}"

    payment_method_id = getattr(data, "payment_method_id", None)
    if payment_method_id and stripe_service.configured():
        # `subtotal` is the pre-tax list price this customer was actually quoted
        # when they picked the plan. Billing them the amount they saw matters
        # more than the catalogue's current value, which the super admin may
        # have changed since.
        result = await stripe_service.create_subscription(
            email=signup["email"],
            full_name=signup["full_name"],
            plan_code=plan_code,
            billing_cycle=billing_cycle,
            payment_method_id=payment_method_id,
            idempotency_key=idempotency_key,
            amount=plan.get("subtotal"),
            plan_name=(await plan_by_code(plan_code)).get("name"),
        )
        if result["success"] or result.get("subscription_id"):
            return result
        logger.error("Subscription creation failed for %s: %s",
                     signup["email"], result.get("message"))
        return result

    if payment_method_id:
        # The caller did the right thing; the server is not set up for it yet.
        logger.warning(
            "payment_method_id supplied but Stripe is not configured - refusing "
            "to fall back to a one-off charge that would never renew",
        )
        return {"success": False, "customer_id": None, "subscription_id": None,
                "reference": None,
                "message": ("Recurring billing is not configured. Set a real "
                            "STRIPE_SECRET_KEY to accept subscriptions.")}

    # A live Stripe account rejects raw card numbers outright, so failing here
    # with an actionable message beats forwarding the card and relaying
    # Stripe's "sending credit card numbers directly is unsafe".
    if stripe_service.configured():
        return {
            "success": False, "customer_id": None, "subscription_id": None,
            "reference": None,
            "message": ("This workspace bills through Stripe, which will not "
                        "accept a raw card number. Tokenise the card in the "
                        "client and send payment_method_id instead."),
        }

    charge = charge_card(data, plan["total_due_today"], idempotency_key=idempotency_key)
    return {**charge, "customer_id": None, "subscription_id": None}


async def _rollback_provisioning(tenant_id: Optional[str]) -> None:
    """Undo a half-finished workspace so the signup can be retried cleanly.

    Best effort: each step is independent, and a failure here must not mask the
    original error that triggered the rollback.
    """
    if not tenant_id:
        return
    db = platform_db()
    for label, action in (
        ("tenant database", lambda: drop_tenant_db(tenant_id)),
        ("tenant record", lambda: db.tenants.delete_one({"_id": oid(tenant_id)})),
        ("directory entries", lambda: db.user_directory.delete_many({"tenant_id": tenant_id})),
        ("subscription", lambda: db.subscriptions.delete_many({"tenant_id": tenant_id})),
    ):
        try:
            await action()
        except Exception:  # noqa: BLE001 - keep unwinding, report at the end
            logger.exception("Rollback could not remove the %s for %s", label, tenant_id)


def charge_card(data, amount: float, idempotency_key: Optional[str] = None) -> Dict[str, Any]:
    """Charge card using Stripe API.

    ``idempotency_key`` makes a retry reuse the original charge rather than
    taking the money twice - Stripe replays the first result for 24 hours.
    """
    import stripe
    from app.core.config import settings

    stripe.api_key = getattr(settings, "STRIPE_SECRET_KEY", "sk_test_51MockupKeyHereForSafety")
    try:
        # Create a Token from raw card data (in production this should be done on client,
        # but to match current backend signup flow API parameters, we do it via token API).
        # We strip non-digits from card_number
        card_num = "".join(c for c in data.card_number if c.isdigit())
        exp_month, exp_year = data.expiry.split("/")
        # format year as 4 digits
        if len(exp_year) == 2:
            exp_year = f"20{exp_year}"

        token_res = stripe.Token.create(
            card={
                "number": card_num,
                "exp_month": int(exp_month),
                "exp_year": int(exp_year),
                "cvc": data.cvc,
                "name": data.name_on_card,
            },
        )

        options = {"idempotency_key": idempotency_key} if idempotency_key else {}
        charge = stripe.Charge.create(
            amount=int(amount * 100),  # Stripe accepts cents
            currency="usd",
            source=token_res.id,
            description="WebImove Subscription Activation",
            **options,
        )
        return {"success": True, "message": "ok", "reference": charge.id}
    except stripe.error.CardError as e:
        return {"success": False, "message": e.user_message or "Card declined", "reference": None}
    except Exception as e:
        return {"success": False, "message": str(e), "reference": None}


# --------------------------------------------------------------------------- #
# Login / tokens
# --------------------------------------------------------------------------- #
async def _store_refresh(user_id: str, tenant_id: Optional[str],
                         session: Optional[Dict[str, Any]] = None) -> str:
    """One row per signed-in device — this is what Security.tsx lists and revokes."""
    token = create_refresh_token(user_id=user_id, tenant_id=tenant_id)
    await platform_db().refresh_tokens.insert_one({
        "token": token,
        "user_id": user_id,
        "tenant_id": tenant_id,
        "ip": (session or {}).get("ip"),
        "user_agent": (session or {}).get("user_agent"),
        "device": (session or {}).get("device"),
        "created_at": utcnow(),
        "expires_at": utcnow() + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS),
    })
    return token


async def login(email: str, password: str, role: Optional[LoginPortalRole] = None,
                session: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Sign in with an email and a password.

    ``role`` is what the caller *claims* the account is, from a Select Your Role
    screen; sending it makes this endpoint refuse an account of any other kind.
    Callers with a single sign-in box omit it, and the account's own role — which
    the directory already records — is used instead. A platform administrator
    signing in without a claimed role is handed to ``platform_login``, so one
    box can be the whole front door.
    """
    db = platform_db()
    email = email.lower().strip()
    ip = (session or {}).get("ip")

    # Checked before any password work: a locked pair costs one indexed read,
    # not a bcrypt round.
    await throttle.ensure_not_locked(throttle.LOGIN, email, ip)

    async def _fail(detail: str = "Email or password is incorrect") -> None:
        await throttle.register_failure(throttle.LOGIN, email, ip)
        await audit.record(action=AuditAction.LOGIN_FAILED, actor_email=email,
                           ip=ip,
                           user_agent=(session or {}).get("user_agent"),
                           meta={"role": role.value if role else None})
        raise Unauthorized(detail)

    if await db.platform_admins.find_one({"email": email}):
        if role is not None:
            raise Unauthorized(
                "Use the Platform administration sign-in for this account"
            )
        return await platform_login(email, password, session=session)

    entry = await db.user_directory.find_one({"email": email})
    if not entry:
        await _fail()

    try:
        entry_role = Role(entry["role"])
    except (KeyError, ValueError):
        await _fail()
    if role is not None and entry_role not in LOGIN_PORTAL_ROLE_MAP[role]:
        label = role.value.replace("_", " ")
        await _fail(f"This account is not registered as a {label}")

    tenant = await db.tenants.find_one({"_id": oid(entry["tenant_id"])})
    if not tenant:
        raise Unauthorized("Workspace not found")

    tdb = tenant_db(entry["tenant_id"])
    user = await tdb.users.find_one({"_id": oid(entry["user_id"])})
    if not user or not verify_password(password, user.get("password_hash") or ""):
        await _fail()

    user_role = Role(user["role"])
    if role is not None and user_role not in LOGIN_PORTAL_ROLE_MAP[role]:
        label = role.value.replace("_", " ")
        await _fail(f"This account is not registered as a {label}")

    if user["status"] == UserStatus.INVITED.value:
        raise Unauthorized("Accept your invitation email before signing in")
    if user["status"] == UserStatus.SUSPENDED.value:
        raise Unauthorized("This account is suspended")
    if (user["status"] == UserStatus.PENDING_VERIFICATION.value
            or not user.get("email_verified", True)):
        raise Unauthorized(
            "Verify your email before signing in. "
            "Use /auth/verify-email/resend to get a new code."
        )

    tenant_status = tenant.get("status")
    if tenant_status == TenantStatus.AWAITING_APPROVAL.value:
        raise Unauthorized(
            "Your organization is awaiting platform approval. "
            "You can sign in once a platform administrator verifies it."
        )
    if tenant_status == TenantStatus.SUSPENDED.value:
        raise Unauthorized("This organization has been suspended")
    if tenant_status == TenantStatus.EXPIRED.value:
        raise Unauthorized("This organization's subscription has expired")
    if tenant_status == TenantStatus.CANCELLED.value:
        raise Unauthorized("This organization has been cancelled")

    # The credentials were right, so the failure counter for this pair is spent.
    # Done before the 2FA branch below, which returns early.
    await throttle.clear(throttle.LOGIN, email, ip)

    await tdb.login_history.insert_one({
        "user_id": str(user["_id"]),
        "ip": ip,
        "user_agent": (session or {}).get("user_agent"),
        "successful": True,
        "portal_role": (role.value if role else user_role.value),
        "created_at": utcnow(),
    })
    await audit.record(action=AuditAction.LOGIN, actor_id=str(user["_id"]),
                       actor_email=email, actor_role=user["role"],
                       tenant_id=entry["tenant_id"], ip=(session or {}).get("ip"),
                       meta={"portal_role": (role.value if role else user_role.value)})

    if user.get("two_factor_enabled"):
        await otp_service.issue_otp(email=email, purpose=OtpPurpose.LOGIN,
                                    tenant_id=entry["tenant_id"])
        return {
            "success": True,
            "message": "Two-factor authentication required",
            "two_factor_required": True,
            "challenge_token": create_onboarding_token(signup_id=str(user["_id"]),
                                                       step="two_factor"),
            "method": user.get("two_factor_method", "email"),
            "role": user_role,
        }

    await tdb.users.update_one({"_id": user["_id"]},
                               {"$set": {"last_login_at": utcnow()}})
    return await _token_response(user, user_role, entry["tenant_id"], tenant,
                                 session=session)


async def platform_login(email: str, password: str, trust_device: bool = True,
                         session: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Platform administration sign-in — only ``platform_admins``."""
    db = platform_db()
    email = email.lower().strip()
    ip = (session or {}).get("ip")

    # The platform admin owns every organization on the instance, so this login
    # is the highest-value target in the system.
    await throttle.ensure_not_locked(throttle.PLATFORM_LOGIN, email, ip)

    admin = await db.platform_admins.find_one({"email": email})
    if not admin or not verify_password(password, admin.get("password_hash") or ""):
        await throttle.register_failure(throttle.PLATFORM_LOGIN, email, ip)
        await audit.record(action=AuditAction.LOGIN_FAILED, actor_email=email,
                           ip=ip,
                           user_agent=(session or {}).get("user_agent"),
                           meta={"portal": "platform"})
        raise Unauthorized("Administrator email or password is incorrect")

    if admin.get("status") == UserStatus.SUSPENDED.value:
        raise Unauthorized("This administrator account is suspended")

    await throttle.clear(throttle.PLATFORM_LOGIN, email, ip)

    await audit.record(action=AuditAction.LOGIN, actor_id=str(admin["_id"]),
                       actor_email=email, actor_role="super_admin",
                       ip=(session or {}).get("ip"),
                       meta={"portal": "platform", "trust_device": trust_device})
    tokens = await _token_response(admin, Role.SUPER_ADMIN, None, session=session)
    tokens["message"] = "Signed in to Platform administration"
    tokens["admin_role"] = admin.get("admin_role", "super_admin")
    tokens["trust_device"] = trust_device
    return tokens


async def verify_login_2fa(email: str, code: str,
                           role: Optional[LoginPortalRole] = None,
                           session: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Second leg of a 2FA sign-in.

    Re-checks the claimed portal role when step one sent one, so the second leg
    cannot be used to slip into a portal the first leg refused.
    """
    db = platform_db()
    email = email.lower().strip()
    await otp_service.verify_otp(email=email, purpose=OtpPurpose.LOGIN, code=code)

    entry = await db.user_directory.find_one({"email": email})
    if not entry:
        raise Unauthorized("Account not found")
    # Only a caller that named a portal in step one gets held to it here.
    allowed_roles = LOGIN_PORTAL_ROLE_MAP[role] if role else None
    try:
        if allowed_roles and Role(entry["role"]) not in allowed_roles:
            raise Unauthorized(f"This account is not registered as a {role.value}")
    except (KeyError, ValueError) as exc:
        raise Unauthorized("Account not found") from exc

    tdb = tenant_db(entry["tenant_id"])
    user = await tdb.users.find_one({"_id": oid(entry["user_id"])})
    if not user:
        raise Unauthorized("Account not found")
    try:
        user_role = Role(user["role"])
    except (KeyError, ValueError) as exc:
        raise Unauthorized("Account not found") from exc
    if allowed_roles and user_role not in allowed_roles:
        raise Unauthorized(f"This account is not registered as a {role.value}")
    tenant = await db.tenants.find_one({"_id": oid(entry["tenant_id"])})
    await tdb.users.update_one({"_id": user["_id"]}, {"$set": {"last_login_at": utcnow()}})
    return await _token_response(user, user_role, entry["tenant_id"], tenant,
                                 session=session)


async def _token_response(user: dict, role: Role, tenant_id: Optional[str],
                          tenant: Optional[dict] = None,
                          session: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    user_id = str(user["_id"])
    profile = serialize({k: v for k, v in user.items() if k != "password_hash"})
    if tenant:
        profile["organization"] = {
            "id": str(tenant["_id"]),
            "name": tenant["name"],
            "status": tenant["status"],
            "plan_code": tenant.get("plan_code"),
        }
    # Partners and clients need to know which consultant they sit under, immediately.
    consultant_id = user.get("consultant_id")
    if tenant_id and consultant_id:
        lookup = await consultant_map(tenant_db(tenant_id), [consultant_id])
        profile["consultant"] = lookup.get(consultant_id)
    return {
        "success": True,
        "message": "Signed in successfully",
        "two_factor_required": False,
        "access_token": create_access_token(
            user_id=user_id, role=role.value, tenant_id=tenant_id, email=user["email"]
        ),
        "refresh_token": await _store_refresh(user_id, tenant_id, session),
        "token_type": "bearer",
        "expires_in": settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        "role": role,
        "tenant_id": tenant_id,
        "consultant_id": consultant_id,
        "user": profile,
    }


async def refresh(token: str) -> Dict[str, Any]:
    db = platform_db()
    stored = await db.refresh_tokens.find_one({"token": token})
    if not stored:
        raise Unauthorized("Refresh token is not recognised")
    try:
        payload = decode_token(token, "refresh")
    except ValueError as exc:
        await db.refresh_tokens.delete_one({"token": token})
        raise Unauthorized(str(exc)) from exc

    tenant_id = payload.get("tenant_id")
    if tenant_id:
        user = await tenant_db(tenant_id).users.find_one({"_id": oid(payload["sub"])})
        tenant = await db.tenants.find_one({"_id": oid(tenant_id)})
        role = Role(user["role"])
    else:
        user = await db.platform_admins.find_one({"_id": oid(payload["sub"])})
        tenant, role = None, Role.SUPER_ADMIN
    if not user:
        raise Unauthorized("Account no longer exists")

    await db.refresh_tokens.delete_one({"token": token})
    return await _token_response(user, role, tenant_id, tenant)


async def logout(token: str) -> None:
    await platform_db().refresh_tokens.delete_one({"token": token})


# --------------------------------------------------------------------------- #
# Password reset
# --------------------------------------------------------------------------- #
async def forgot_password(email: str) -> Dict[str, Any]:
    db = platform_db()
    email = email.lower().strip()
    known = await db.user_directory.find_one({"email": email}) or await db.platform_admins.find_one(
        {"email": email}
    )
    if not known:
        # Do not leak which emails exist.
        return {"expires_in": settings.OTP_EXPIRE_MINUTES * 60,
                "resend_in": settings.OTP_RESEND_COOLDOWN_SECONDS, "debug_code": None}
    return await otp_service.issue_otp(email=email, purpose=OtpPurpose.PASSWORD_RESET)


async def reset_password(email: str, code: str, new_password: str) -> None:
    db = platform_db()
    email = email.lower().strip()
    await otp_service.verify_otp(email=email, purpose=OtpPurpose.PASSWORD_RESET, code=code)
    hashed = hash_password(new_password)

    entry = await db.user_directory.find_one({"email": email})
    if entry:
        await tenant_db(entry["tenant_id"]).users.update_one(
            {"_id": oid(entry["user_id"])},
            {"$set": {"password_hash": hashed, "updated_at": utcnow()}},
        )
    else:
        await db.platform_admins.update_one({"email": email}, {"$set": {"password_hash": hashed}})
    await db.refresh_tokens.delete_many({"user_id": entry["user_id"] if entry else None})


async def change_password(user, current: str, new: str) -> None:
    if not verify_password(current, user.raw.get("password_hash") or ""):
        raise BadRequest("Current password is incorrect")
    db = tenant_db(user.tenant_id) if user.tenant_id else None
    hashed = hash_password(new)
    if db is not None:
        await db.users.update_one({"_id": oid(user.id)},
                                  {"$set": {"password_hash": hashed, "updated_at": utcnow()}})
    else:
        await platform_db().platform_admins.update_one({"_id": oid(user.id)},
                                                       {"$set": {"password_hash": hashed}})


# --------------------------------------------------------------------------- #
# Client self-registration under a chosen consultant organization
# --------------------------------------------------------------------------- #
async def list_organizations(search: Optional[str] = None) -> list:
    db = platform_db()
    query: Dict[str, Any] = {"status": TenantStatus.ACTIVE.value}
    if search:
        query["name"] = {"$regex": search, "$options": "i"}
    out = []
    async for t in db.tenants.find(query).sort("name", 1).limit(100):
        tid = str(t["_id"])
        partner_count = await tenant_db(tid).users.count_documents({"role": Role.PARTNER.value})
        out.append({
            "id": tid,
            "name": t["name"],
            "country": t.get("country", ""),
            "consultant_name": t.get("owner_name", ""),
            "logo_url": t.get("logo_url"),
            "partner_count": partner_count,
        })
    return out


async def list_public_consultants(search: Optional[str] = None,
                                  organization_id: Optional[str] = None) -> list:
    """
    The directory a client picks from at signup. Spans every active organization,
    because a client chooses a consultant, not a firm.
    """
    db = platform_db()
    query: Dict[str, Any] = {"status": TenantStatus.ACTIVE.value}
    if organization_id:
        query["_id"] = oid(organization_id)

    out = []
    async for tenant in db.tenants.find(query).sort("name", 1).limit(200):
        tid = str(tenant["_id"])
        tdb = tenant_db(tid)
        user_query: Dict[str, Any] = {
            "role": {"$in": [Role.CONSULTANT_OWNER.value, Role.CONSULTANT.value]},
            "status": UserStatus.ACTIVE.value,
        }
        if search:
            user_query["$or"] = [
                {"full_name": {"$regex": search, "$options": "i"}},
                {"title": {"$regex": search, "$options": "i"}},
            ]
        async for c in tdb.users.find(user_query):
            cid = str(c["_id"])
            out.append({
                "id": cid,
                "full_name": c.get("full_name"),
                "title": c.get("title") or "Consultant",
                "avatar_url": c.get("avatar_url"),
                "is_owner": c.get("role") == Role.CONSULTANT_OWNER.value,
                "organization": {"id": tid, "name": tenant["name"],
                                 "country": tenant.get("country")},
                "active_clients": await tdb.users.count_documents(
                    {"role": Role.CLIENT.value, "consultant_id": cid}),
            })
    if search:
        needle = search.lower()
        out = [c for c in out
               if needle in (c["full_name"] or "").lower()
               or needle in (c["organization"]["name"] or "").lower()]
    return out


async def register_client(data) -> Dict[str, Any]:
    """
    Client self-signup. They pick a CONSULTANT; the organization follows from that,
    and they land in that consultant's workspace under that consultant.
    """
    db = platform_db()
    email = data.email.lower()
    if await db.user_directory.find_one({"email": email}):
        raise Conflict("An account with this email already exists")

    tenant, consultant_id = await _resolve_signup_target(data)
    tid = str(tenant["_id"])
    tdb = tenant_db(tid)
    now = utcnow()
    consultant = await tdb.users.find_one({"_id": oid(consultant_id)})

    user = {
        "email": email,
        "full_name": data.full_name,
        "mobile": data.mobile,
        "password_hash": hash_password(data.password),
        "role": Role.CLIENT.value,
        "status": UserStatus.PENDING_VERIFICATION.value,
        "email_verified": False,
        "passport_number": getattr(data, "passport_number", None),
        "nationality": data.nationality,
        "destination_country": getattr(data, "destination_country", None),
        "preferred_immigration_type": getattr(data, "preferred_immigration_type", None),
        "language": data.language,
        "country_of_residence": data.country_of_residence,
        "consultant_id": consultant_id,
        "created_at": now,
        "updated_at": now,
    }
    user_id = str((await tdb.users.insert_one(user)).inserted_id)
    await db.user_directory.insert_one({
        "email": email, "tenant_id": tid, "user_id": user_id,
        "role": Role.CLIENT.value, "created_at": now,
    })
    issued = await otp_service.issue_otp(email=email, purpose=OtpPurpose.EMAIL_VERIFICATION,
                                         tenant_id=tid)
    return {
        "user_id": user_id,
        "tenant_id": tid,
        "organization_name": tenant["name"],
        "consultant": {
            "id": consultant_id,
            "full_name": (consultant or {}).get("full_name"),
            "title": (consultant or {}).get("title") or "Consultant",
        },
        "next_step": "verify_email",
        **issued,
    }


# --------------------------------------------------------------------------- #
# Client 7-Step Onboarding Flow
# Step 1: Account
# Step 2: Verification
# Step 3: Create Password
# Step 4: Immigration Profile
# Step 5: Choose Consultant / Organization
# Step 6: Confirm & Submit Request
# Step 7: Agreements & Finalize Account
# --------------------------------------------------------------------------- #
async def start_client_signup(data) -> Dict[str, Any]:
    db = platform_db()
    email = data.email.lower().strip()

    if await db.user_directory.find_one({"email": email}):
        raise Conflict("An account with this email already exists")

    await db.signups.delete_many({"email": email, "completed": {"$ne": True}})
    doc = {
        "email": email,
        "full_name": data.full_name,
        "mobile": data.mobile,
        "dob": data.dob,
        "profile_photo_url": data.profile_photo_url,
        "role": Role.CLIENT.value,
        "step": "verification",
        "email_verified": False,
        "completed": False,
        "created_at": utcnow(),
    }
    result = await db.signups.insert_one(doc)

    issued = await otp_service.issue_otp(email=email, purpose=OtpPurpose.EMAIL_VERIFICATION)

    return {
        "client_onboarding_token": create_onboarding_token(
            signup_id=str(result.inserted_id), step="verification"
        ),
        "step": "account",
        "next_step": "verification",
        "detail": f"Verification code sent to {email}",
        **issued,
    }


async def verify_client_signup_email(token: str, code: str) -> Dict[str, Any]:
    db = platform_db()
    signup = await _load_signup(token)

    await otp_service.verify_otp(
        email=signup["email"], purpose=OtpPurpose.EMAIL_VERIFICATION, code=code
    )

    await db.signups.update_one(
        {"_id": signup["_id"]},
        {"$set": {"email_verified": True, "step": "create_password"}},
    )

    return {
        "client_onboarding_token": create_onboarding_token(
            signup_id=str(signup["_id"]), step="create_password"
        ),
        "step": "verification",
        "next_step": "create_password",
    }


async def resend_client_signup_otp(token: str) -> Dict[str, Any]:
    signup = await _load_signup(token)
    return await otp_service.issue_otp(
        email=signup["email"],
        purpose=OtpPurpose.EMAIL_VERIFICATION,
        tenant_id=signup.get("tenant_id"),
    )


async def set_client_password(token: str, data) -> Dict[str, Any]:
    db = platform_db()
    signup = await _load_signup(token)

    if not signup.get("email_verified"):
        raise BadRequest("Please verify your email before setting a password")

    await db.signups.update_one(
        {"_id": signup["_id"]},
        {"$set": {
            "password_hash": hash_password(data.password),
            "step": "immigration",
        }},
    )

    return {
        "client_onboarding_token": create_onboarding_token(
            signup_id=str(signup["_id"]), step="immigration"
        ),
        "step": "create_password",
        "next_step": "immigration",
    }


async def set_client_immigration(token: str, data) -> Dict[str, Any]:
    db = platform_db()
    signup = await _load_signup(token)

    await db.signups.update_one(
        {"_id": signup["_id"]},
        {"$set": {
            "immigration_profile": {
                "passport_number": data.passport_number,
                "nationality": data.nationality,
                "destination_country": data.destination_country,
                "preferred_immigration_type": data.preferred_immigration_type,
                "country_of_residence": data.country_of_residence,
            },
            "step": "consultant",
        }},
    )

    return {
        "client_onboarding_token": create_onboarding_token(
            signup_id=str(signup["_id"]), step="consultant"
        ),
        "step": "immigration",
        "next_step": "consultant",
    }


async def set_client_consultant(token: str, data) -> Dict[str, Any]:
    db = platform_db()
    signup = await _load_signup(token)

    tenant, consultant_id = await _resolve_signup_target(data)
    tid = str(tenant["_id"])
    tdb = tenant_db(tid)
    consultant = await tdb.users.find_one({"_id": oid(consultant_id)}) if consultant_id else None

    imm = signup.get("immigration_profile", {})

    await db.signups.update_one(
        {"_id": signup["_id"]},
        {"$set": {
            "tenant_id": tid,
            "consultant_id": consultant_id,
            "step": "confirm",
        }},
    )

    return {
        "client_onboarding_token": create_onboarding_token(
            signup_id=str(signup["_id"]), step="confirm"
        ),
        "step": "consultant",
        "next_step": "confirm",
        "preview": {
            "organization_name": tenant["name"],
            "consultant_name": (consultant or {}).get("full_name") or tenant.get("owner_name", "Consultant"),
            "consultant_country": tenant.get("country", ""),
            "immigration_type": imm.get("preferred_immigration_type"),
            "nationality": imm.get("nationality"),
            "current_country": imm.get("country_of_residence"),
            "passport_number": imm.get("passport_number"),
        },
    }


async def confirm_client_signup(token: str, data) -> Dict[str, Any]:
    db = platform_db()
    signup = await _load_signup(token)

    if not signup.get("tenant_id") or not signup.get("consultant_id"):
        raise BadRequest("Please choose a consultant or organization first")

    await db.signups.update_one(
        {"_id": signup["_id"]},
        {"$set": {
            "gdpr_consent": data.gdpr_consent,
            "accepted_terms_conditions": data.accept_terms_conditions,
            "step": "agreements",
        }},
    )

    return {
        "client_onboarding_token": create_onboarding_token(
            signup_id=str(signup["_id"]), step="agreements"
        ),
        "step": "confirm",
        "next_step": "agreements",
    }


async def finalize_client_signup(token: str, data, session: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    db = platform_db()
    signup = await _load_signup(token)

    if not signup.get("tenant_id") or not signup.get("consultant_id"):
        raise BadRequest("Signup process is incomplete. Missing consultant context.")

    tid = signup["tenant_id"]
    consultant_id = signup["consultant_id"]
    tdb = tenant_db(tid)
    now = utcnow()
    imm = signup.get("immigration_profile", {})

    consultant = await tdb.users.find_one({"_id": oid(consultant_id)})
    tenant = await db.tenants.find_one({"_id": oid(tid)})

    user = {
        "email": signup["email"],
        "full_name": signup["full_name"],
        "mobile": signup["mobile"],
        "dob": signup.get("dob"),
        "profile_photo_url": signup.get("profile_photo_url"),
        "password_hash": signup["password_hash"],
        "role": Role.CLIENT.value,
        "status": UserStatus.ACTIVE.value,
        "email_verified": True,
        "passport_number": imm.get("passport_number"),
        "nationality": imm.get("nationality"),
        "destination_country": imm.get("destination_country"),
        "preferred_immigration_type": imm.get("preferred_immigration_type"),
        "country_of_residence": imm.get("country_of_residence"),
        "consultant_id": consultant_id,
        "agreements": data.model_dump(),
        "created_at": now,
        "updated_at": now,
    }
    user_id = str((await tdb.users.insert_one(user)).inserted_id)

    await db.user_directory.insert_one({
        "email": signup["email"],
        "tenant_id": tid,
        "user_id": user_id,
        "role": Role.CLIENT.value,
        "created_at": now,
    })

    # The goal captured at sign-up becomes the client's first request, in the
    # same shape POST /requests writes — otherwise it would read as an untitled
    # request with no reference everywhere it appears.
    seq = await next_sequence(db, "request", start=200)
    visa_type = imm.get("preferred_immigration_type") or "Immigration Advice"
    request_doc = {
        "reference": build_reference("REQ", seq),
        "visa_type": visa_type,
        "destination_country": imm.get("destination_country") or "",
        "origin_country": imm.get("country_of_residence"),
        "purpose": f"{visa_type} enquiry raised during registration.",
        "additional_information": None,
        "client_notes": None,
        "preferred_appointment": "Flexible",
        "review_notes": None,
        "status": RequestStatus.NEW.value,
        "client_id": user_id,
        "client_name": signup.get("full_name"),
        "consultant_id": consultant_id,
        "attached_files": [],
        "is_draft": False,
        "case_id": None,
        "created_at": now,
        "updated_at": now,
    }
    request_id = str((await tdb.requests.insert_one(request_doc)).inserted_id)

    await notify(tdb, user_ids=[consultant_id], type=NotificationType.REQUEST_SUBMITTED,
                 title=f"{signup.get('full_name')} joined and raised a request",
                 body=f"{visa_type} · {request_doc['reference']}",
                 data={"request_id": request_id})

    await db.signups.update_one(
        {"_id": signup["_id"]},
        {"$set": {"completed": True, "user_id": user_id}}
    )

    created_user = await tdb.users.find_one({"_id": oid(user_id)})
    tokens = await _token_response(
        created_user, Role.CLIENT, tid, tenant=tenant, session=session
    )

    return {
        "token_pair": tokens,
        "request_summary": {
            "status": "Waiting for review",
            "request_id": request_id,
            "request_number": request_doc["reference"],
            "organization_name": tenant["name"] if tenant else "",
            "consultant_name": (consultant or {}).get("full_name") or "",
            "message": f"Your account is created and linked to {tenant['name'] if tenant else ''}. { (consultant or {}).get('full_name', '') } has received your immigration request.",
            "next_steps": "Consultant review -> document requests -> case created",
        },
    }




async def _resolve_signup_target(data):
    """Accepts consultant_id (preferred) or organization_id (falls back to the owner)."""
    db = platform_db()
    consultant_id = getattr(data, "consultant_id", None)
    organization_id = getattr(data, "organization_id", None)

    if not consultant_id and not organization_id:
        raise BadRequest("Choose a consultant to work with")

    if consultant_id:
        query = {"status": TenantStatus.ACTIVE.value}
        if organization_id:
            query["_id"] = oid(organization_id)
        async for tenant in db.tenants.find(query):
            tdb = tenant_db(str(tenant["_id"]))
            found = await tdb.users.find_one({
                "_id": oid(consultant_id),
                "role": {"$in": [Role.CONSULTANT_OWNER.value, Role.CONSULTANT.value]},
                "status": UserStatus.ACTIVE.value,
            })
            if found:
                return tenant, consultant_id
        raise NotFound("That consultant is not available")

    tenant = await db.tenants.find_one({"_id": oid(organization_id)})
    if not tenant:
        raise NotFound("That organization does not exist")
    if tenant["status"] != TenantStatus.ACTIVE.value:
        raise BadRequest("That organization is not accepting clients right now")
    owner = await tenant_db(str(tenant["_id"])).users.find_one(
        {"role": Role.CONSULTANT_OWNER.value})
    return tenant, str(owner["_id"]) if owner else None


async def resend_verification(email: str) -> Dict[str, Any]:
    db = platform_db()
    email = email.lower().strip()
    entry = await db.user_directory.find_one({"email": email})
    if not entry:
        # Do not reveal which emails exist.
        return {"expires_in": settings.OTP_EXPIRE_MINUTES * 60,
                "resend_in": settings.OTP_RESEND_COOLDOWN_SECONDS, "debug_code": None}
    return await otp_service.issue_otp(email=email,
                                       purpose=OtpPurpose.EMAIL_VERIFICATION,
                                       tenant_id=entry["tenant_id"])


async def verify_account_email(email: str, code: str) -> Dict[str, Any]:
    db = platform_db()
    email = email.lower().strip()
    await otp_service.verify_otp(email=email, purpose=OtpPurpose.EMAIL_VERIFICATION, code=code)
    entry = await db.user_directory.find_one({"email": email})
    if not entry:
        raise NotFound("Account not found")
    tdb = tenant_db(entry["tenant_id"])
    await tdb.users.update_one(
        {"_id": oid(entry["user_id"])},
        {"$set": {"email_verified": True, "status": UserStatus.ACTIVE.value,
                  "updated_at": utcnow()}},
    )
    user = await tdb.users.find_one({"_id": oid(entry["user_id"])})
    tenant = await db.tenants.find_one({"_id": oid(entry["tenant_id"])})
    return await _token_response(user, Role(user["role"]), entry["tenant_id"], tenant)


async def preview_invite(token: str) -> Dict[str, Any]:
    """What the accept screen shows before asking for a password."""
    return (await invites.lookup(token))["preview"]


async def accept_invite(token: str, password: str,
                        session: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Partner or team member sets a password from the emailed link.

    The account was created by a consultant, so ownership is already recorded on the
    user document - accepting activates it, it does not re-decide who they belong to.
    """
    found = await invites.consume(token)
    entry, tenant = found["entry"], found["tenant"]
    tdb = tenant_db(entry["tenant_id"])
    now = utcnow()

    update = {
        "password_hash": hash_password(password),
        "status": UserStatus.ACTIVE.value,
        "email_verified": True,
        "joined_at": now,
        "updated_at": now,
    }
    # Belt and braces: if the record predates ownership, bind it to the inviter now.
    if entry.get("invited_by") and not found["user"].get("consultant_id"):
        if Role(entry["role"]) in {Role.PARTNER, Role.CLIENT}:
            update["consultant_id"] = entry["invited_by"]

    await tdb.users.update_one({"_id": oid(entry["user_id"])}, {"$set": update})
    if Role(entry["role"]) == Role.PARTNER and entry.get("invited_by"):
        await link_partner_to_consultant(tdb, entry["user_id"], entry["invited_by"])

    await audit.record(action=AuditAction.USER_INVITED, actor_id=entry["user_id"],
                       actor_email=entry["email"], actor_role=entry["role"],
                       tenant_id=entry["tenant_id"], subject="accepted invitation")

    user = await tdb.users.find_one({"_id": oid(entry["user_id"])})
    return await _token_response(user, Role(user["role"]), entry["tenant_id"], tenant,
                                 session=session)

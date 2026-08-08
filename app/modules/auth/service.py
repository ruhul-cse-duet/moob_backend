from datetime import timedelta
from typing import Any, Dict, Optional

from app.core.enums import (
    AuditAction,
    BillingCycle,
    OtpPurpose,
    PlanCode,
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
from app.core.utils import oid, random_token, serialize, slugify_db, utcnow
from app.db.indexes import ensure_tenant_indexes
from app.db.mongo import platform_db, tenant_db
from app.modules.subscriptions.plans import order_summary
from app.services import audit
from app.services import invites
from app.services import otp as otp_service
from app.services.ownership import consultant_map, link_partner_to_consultant


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

    summary = order_summary(data.plan_code, data.billing_cycle)
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

    charge = _fake_charge(data, signup["plan"]["total_due_today"])
    if not charge["success"]:
        raise BadRequest(charge["message"])

    org = signup["organization"]
    plan = signup["plan"]
    now = utcnow()

    tenant_doc = {
        "name": org["name"],
        "slug": org["slug"],
        "business_type": org["business_type"],
        "country": org["country"],
        "office_address": org["office_address"],
        "owner_email": signup["email"],
        "owner_name": signup["full_name"],
        "status": TenantStatus.ACTIVE.value,
        "plan_code": plan["code"],
        "billing_cycle": plan["billing_cycle"],
        "referral_code": random_token(8).upper(),
        "activated_at": now,
        "renews_on": plan["renews_on"],
        "created_at": now,
        "updated_at": now,
    }
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
        "payment_reference": charge["reference"],
        "card_last4": data.card_number[-4:],
        "created_at": now,
    })
    await db.signups.update_one(
        {"_id": signup["_id"]}, {"$set": {"completed": True, "tenant_id": tenant_id}}
    )
    await audit.record(action=AuditAction.TENANT_CREATED, actor_id=user_id,
                       actor_email=signup["email"], actor_role=Role.CONSULTANT_OWNER.value,
                       tenant_id=tenant_id, subject=org["name"],
                       detail=f"{plan['code']} / {plan['billing_cycle']}")

    return {
        "tenant_id": tenant_id,
        "organization_name": org["name"],
        "plan_code": PlanCode(plan["code"]),
        "renews_on": plan["renews_on"],
        "referral_code": tenant_doc["referral_code"],
        "access_token": create_access_token(
            user_id=user_id, role=Role.CONSULTANT_OWNER.value,
            tenant_id=tenant_id, email=signup["email"],
        ),
        "refresh_token": await _store_refresh(user_id, tenant_id),
    }


def _fake_charge(data, amount: float) -> Dict[str, Any]:
    """Charge card using Stripe API."""
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

        charge = stripe.Charge.create(
            amount=int(amount * 100),  # Stripe accepts cents
            currency="usd",
            source=token_res.id,
            description="WebImove Subscription Activation",
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


async def login(email: str, password: str,
                session: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    db = platform_db()
    email = email.lower().strip()

    async def _fail() -> None:
        await audit.record(action=AuditAction.LOGIN_FAILED, actor_email=email,
                           ip=(session or {}).get("ip"),
                           user_agent=(session or {}).get("user_agent"))
        raise Unauthorized("Email or password is incorrect")

    admin = await db.platform_admins.find_one({"email": email})
    if admin:
        if not verify_password(password, admin["password_hash"]):
            await _fail()
        await audit.record(action=AuditAction.LOGIN, actor_id=str(admin["_id"]),
                           actor_email=email, actor_role="super_admin",
                           ip=(session or {}).get("ip"))
        return await _token_response(admin, Role.SUPER_ADMIN, None, session=session)

    entry = await db.user_directory.find_one({"email": email})
    if not entry:
        await _fail()

    tenant = await db.tenants.find_one({"_id": oid(entry["tenant_id"])})
    if not tenant:
        raise Unauthorized("Workspace not found")

    tdb = tenant_db(entry["tenant_id"])
    user = await tdb.users.find_one({"_id": oid(entry["user_id"])})
    if not user or not verify_password(password, user.get("password_hash") or ""):
        await _fail()
    if user["status"] == UserStatus.INVITED.value:
        raise Unauthorized("Accept your invitation email before signing in")
    if user["status"] == UserStatus.SUSPENDED.value:
        raise Unauthorized("This account is suspended")
    if (user["status"] == UserStatus.PENDING_VERIFICATION.value
            or not user.get("email_verified", True)):
        # Previously missing: an unverified self-registered client could sign in.
        raise Unauthorized(
            "Verify your email before signing in. "
            "Use /auth/verify-email/resend to get a new code."
        )

    await tdb.login_history.insert_one({
        "user_id": str(user["_id"]),
        "ip": (session or {}).get("ip"),
        "user_agent": (session or {}).get("user_agent"),
        "successful": True,
        "created_at": utcnow(),
    })
    await audit.record(action=AuditAction.LOGIN, actor_id=str(user["_id"]),
                       actor_email=email, actor_role=user["role"],
                       tenant_id=entry["tenant_id"], ip=(session or {}).get("ip"))

    # 2FA gate: hand back a short-lived challenge instead of tokens. [INFERRED]
    if user.get("two_factor_enabled"):
        await otp_service.issue_otp(email=email, purpose=OtpPurpose.LOGIN,
                                    tenant_id=entry["tenant_id"])
        return {
            "two_factor_required": True,
            "challenge_token": create_onboarding_token(signup_id=str(user["_id"]),
                                                       step="two_factor"),
            "method": user.get("two_factor_method", "email"),
        }

    await tdb.users.update_one({"_id": user["_id"]},
                               {"$set": {"last_login_at": utcnow()}})
    return await _token_response(user, Role(user["role"]), entry["tenant_id"], tenant,
                                 session=session)


async def verify_login_2fa(email: str, code: str,
                           session: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Second leg of a 2FA sign-in. [INFERRED]"""
    db = platform_db()
    email = email.lower().strip()
    await otp_service.verify_otp(email=email, purpose=OtpPurpose.LOGIN, code=code)

    entry = await db.user_directory.find_one({"email": email})
    if not entry:
        raise Unauthorized("Account not found")
    tdb = tenant_db(entry["tenant_id"])
    user = await tdb.users.find_one({"_id": oid(entry["user_id"])})
    tenant = await db.tenants.find_one({"_id": oid(entry["tenant_id"])})
    await tdb.users.update_one({"_id": user["_id"]}, {"$set": {"last_login_at": utcnow()}})
    return await _token_response(user, Role(user["role"]), entry["tenant_id"], tenant,
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
        "access_token": create_access_token(
            user_id=user_id, role=role.value, tenant_id=tenant_id, email=user["email"]
        ),
        "refresh_token": await _store_refresh(user_id, tenant_id, session),
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

    # Create immigration request (e.g. REQ-699)
    req_number = f"REQ-{random_token(4).upper()}"
    request_doc = {
        "request_number": req_number,
        "client_id": user_id,
        "consultant_id": consultant_id,
        "immigration_type": imm.get("preferred_immigration_type") or "Immigration Advice",
        "status": RequestStatus.NEW.value,
        "stage": "consultant_review",
        "created_at": now,
        "updated_at": now,
    }
    await tdb.requests.insert_one(request_doc)

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
            "request_number": req_number,
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

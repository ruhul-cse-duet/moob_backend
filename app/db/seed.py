"""What a brand-new database needs before anyone can sign in.

A fresh platform database has no administrator, and every route into the admin
area needs one to already exist. Rather than make that a manual step people
forget, ADMIN_EMAIL and ADMIN_PASSWORD in the environment are enough: boot the
API once and the account is there.
"""
import logging
from typing import Optional

from pymongo.errors import DuplicateKeyError

from app.core.config import settings
from app.core.enums import UserStatus
from app.core.security import hash_password
from app.core.utils import utcnow
from app.db.mongo import platform_db
from app.db.policy_text import SEED_POLICIES, SEED_VERSION

logger = logging.getLogger("app.db.seed")

MIN_PASSWORD_LENGTH = 8


async def seed_platform_admin(
    email: Optional[str] = None,
    password: Optional[str] = None,
    name: Optional[str] = None,
) -> bool:
    """Create the configured platform administrator if it is missing.

    Idempotent, and deliberately create-only: an account that already exists is
    never rewritten. Changing ADMIN_PASSWORD and restarting must not hand the
    platform to whoever can edit the environment - so the boot log says plainly
    that the value was ignored, instead of leaving the operator to guess why
    their new password is refused.

    Returns True only when an account was actually created.
    """
    email = (email or settings.ADMIN_EMAIL).strip().lower()
    password = password or settings.ADMIN_PASSWORD
    name = name or settings.ADMIN_NAME

    if not email or not password:
        # Not configured is not a problem; it just means seeding is off.
        return False

    if len(password) < MIN_PASSWORD_LENGTH:
        logger.error(
            "ADMIN_PASSWORD is shorter than %d characters - no administrator "
            "was created", MIN_PASSWORD_LENGTH,
        )
        return False

    db = platform_db()

    if await db.platform_admins.find_one({"email": email}):
        logger.info(
            "Platform administrator %s already exists - ADMIN_PASSWORD was not "
            "applied. Change it with the forgot-password flow, or from the "
            "admin area as another administrator.", email,
        )
        return False

    # An address the directory already knows belongs to a consultant, partner or
    # client. Sign-in looks at platform_admins first, so seeding here would
    # quietly lock that person out of their own workspace.
    if await db.user_directory.find_one({"email": email}):
        logger.error(
            "ADMIN_EMAIL %s already belongs to a workspace account - no "
            "administrator was created. Use a different address.", email,
        )
        return False

    try:
        await db.platform_admins.insert_one({
            "email": email,
            "full_name": name,
            "password_hash": hash_password(password),
            "status": UserStatus.ACTIVE.value,
            "created_at": utcnow(),
        })
    except DuplicateKeyError:
        # Another worker booted a moment earlier. The unique index is what makes
        # that safe: exactly one of them wins and neither overwrites the other.
        logger.info("Platform administrator %s was created by another worker", email)
        return False

    logger.info("Platform administrator created: %s", email)
    return True


async def seed_policies() -> int:
    """Publish the starting version of each legal policy, once.

    Create-only per kind, and deliberately blind to version numbers: the check
    is "does this kind have any version at all", not "does 1.0 exist". An
    administrator who publishes their own 2.0 and archives ours must not have
    1.0 reappear under them on the next restart.

    Returns how many kinds were seeded.
    """
    db = platform_db()
    seeded = 0

    for kind, (title, body) in SEED_POLICIES.items():
        if await db.policies.find_one({"kind": kind.value}, {"_id": 1}):
            continue
        now = utcnow()
        try:
            await db.policies.insert_one({
                "kind": kind.value,
                "version": SEED_VERSION,
                "title": title,
                "body_markdown": body,
                "effective_from": None,
                "published": True,
                "published_at": now,
                "seeded": True,
                "created_at": now,
                "updated_at": now,
            })
        except DuplicateKeyError:
            # Two workers booting together; whichever lost is fine either way.
            continue
        seeded += 1

    if seeded:
        logger.info("Seeded %d starting legal policies at version %s",
                    seeded, SEED_VERSION)
    return seeded

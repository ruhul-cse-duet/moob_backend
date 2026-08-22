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

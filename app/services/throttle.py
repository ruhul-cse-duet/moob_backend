"""
Brute-force throttling for credential endpoints.

Password checks are the one place where an attacker gets unlimited free guesses:
`verify_password` is deliberately slow, but nothing stopped a script from running
it a few million times against one address. This counts failures per
(scope, subject) in the platform database and locks the pair out for a while once
the count crosses a threshold.

Mongo rather than in-process memory on purpose - the API runs behind more than
one worker, and a counter that lives in a single process resets on every deploy
and is trivially bypassed by hitting a different worker.

The counter is keyed by **email + client IP together**, not by either alone:

* email only  -> anyone can lock a victim out of their own account by failing
                 their login on purpose.
* IP only     -> an office behind one NAT locks itself out.

The document expires by TTL, so nothing has to be swept.
"""
import logging
from datetime import timedelta
from typing import Optional

from app.core.config import settings
from app.core.exceptions import TooManyRequests
from app.core.utils import utcnow
from app.db.mongo import platform_db

logger = logging.getLogger(__name__)

COLLECTION = "auth_throttle"

LOGIN = "login"
PLATFORM_LOGIN = "platform_login"
PASSWORD_RESET = "password_reset"


def _key(scope: str, subject: str, ip: Optional[str]) -> str:
    return f"{scope}:{(subject or '').lower().strip()}|{ip or 'unknown'}"


def _seconds_remaining(locked_until) -> int:
    """Seconds until the lock lifts. Zero or negative once it has.

    Mongo hands back naive UTC datetimes, so match the awareness of whatever was
    stored before subtracting. Must be allowed to go non-positive: clamping this
    to a minimum of one would make every expired lock look like a live one, and
    the lockout would never lift.
    """
    now = utcnow()
    reference = now if locked_until.tzinfo else now.replace(tzinfo=None)
    return int((locked_until - reference).total_seconds())


async def ensure_not_locked(scope: str, subject: str, ip: Optional[str] = None) -> None:
    """Raise 429 when this (subject, ip) pair is currently locked out.

    Call this *before* verifying the password, so a locked account costs an
    attacker a database read rather than a bcrypt round.
    """
    record = await platform_db()[COLLECTION].find_one({"_id": _key(scope, subject, ip)})
    if not record:
        return
    locked_until = record.get("locked_until")
    if not locked_until:
        return
    remaining = _seconds_remaining(locked_until)
    if remaining <= 0:
        # Lock served. The row itself is left to the TTL sweep.
        return
    retry_after = max(1, remaining)
    minutes = max(1, round(retry_after / 60))
    raise TooManyRequests(
        f"Too many failed attempts. Try again in about {minutes} minute"
        f"{'s' if minutes != 1 else ''}.",
        retry_after=retry_after,
    )


async def register_failure(scope: str, subject: str, ip: Optional[str] = None) -> None:
    """Count one failed attempt, and lock the pair out once the limit is reached."""
    now = utcnow()
    window = timedelta(minutes=settings.LOGIN_ATTEMPT_WINDOW_MINUTES)
    lockout = timedelta(minutes=settings.LOGIN_LOCKOUT_MINUTES)
    key = _key(scope, subject, ip)
    db = platform_db()

    record = await db[COLLECTION].find_one({"_id": key})
    first_at = record.get("first_at") if record else None
    failures = int(record.get("failures", 0)) if record else 0
    if first_at is not None:
        reference = now if first_at.tzinfo else now.replace(tzinfo=None)
        if (reference - first_at) > window:
            # The old burst aged out; this attempt starts a fresh window.
            failures, first_at = 0, None

    failures += 1
    update = {
        "scope": scope,
        "subject": (subject or "").lower().strip(),
        "ip": ip,
        "failures": failures,
        "first_at": first_at or now,
        "last_at": now,
        # Keep the row a little past the lockout so the counter is not reset by
        # the TTL sweep the moment a lock expires.
        "expires_at": now + lockout + window,
    }
    if failures >= settings.LOGIN_MAX_ATTEMPTS:
        update["locked_until"] = now + lockout
        logger.warning(
            "Locked out %s for %s after %d failed attempts",
            update["subject"] or "unknown", scope, failures,
        )

    await db[COLLECTION].update_one({"_id": key}, {"$set": update}, upsert=True)


async def clear(scope: str, subject: str, ip: Optional[str] = None) -> None:
    """A successful sign-in wipes the counter for that pair."""
    await platform_db()[COLLECTION].delete_one({"_id": _key(scope, subject, ip)})

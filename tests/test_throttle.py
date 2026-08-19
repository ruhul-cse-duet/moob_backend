"""
Login brute-force throttling.

`verify_password` is slow by design, but slow is not a limit - a script can still
work through a password list given time. These tests pin the two properties that
make the counter useful without making it a weapon:

* it locks after the configured number of failures, and
* it is keyed by email **and** IP, so one attacker cannot lock a victim out of
  their own account from somewhere else.
"""
import pytest
from mongomock_motor import AsyncMongoMockClient

from app.core.config import settings
from app.core.exceptions import TooManyRequests
from app.services import throttle

EMAIL = "sarah@jenkinslaw.com"
ATTACKER_IP = "203.0.113.9"
VICTIM_IP = "198.51.100.4"


@pytest.fixture
def mongo(monkeypatch):
    db = AsyncMongoMockClient()["webimove_platform"]
    monkeypatch.setattr(throttle, "platform_db", lambda: db)
    return db


async def fail_n(times, ip=ATTACKER_IP, scope=throttle.LOGIN, email=EMAIL):
    for _ in range(times):
        await throttle.register_failure(scope, email, ip)


@pytest.mark.asyncio
async def test_allows_attempts_below_the_limit(mongo):
    await fail_n(settings.LOGIN_MAX_ATTEMPTS - 1)
    # Must not raise: a user who mistypes a few times is not an attacker.
    await throttle.ensure_not_locked(throttle.LOGIN, EMAIL, ATTACKER_IP)


@pytest.mark.asyncio
async def test_locks_out_at_the_limit(mongo):
    await fail_n(settings.LOGIN_MAX_ATTEMPTS)
    with pytest.raises(TooManyRequests) as caught:
        await throttle.ensure_not_locked(throttle.LOGIN, EMAIL, ATTACKER_IP)
    assert caught.value.status_code == 429
    assert caught.value.retry_after > 0
    assert "Retry-After" in caught.value.headers


@pytest.mark.asyncio
async def test_lockout_does_not_follow_the_victim_to_their_own_ip(mongo):
    """Keyed by email+IP on purpose.

    Keyed by email alone, anyone who knows an address could lock its owner out
    by failing the login on purpose - a denial of service handed to the attacker.
    """
    await fail_n(settings.LOGIN_MAX_ATTEMPTS + 3, ip=ATTACKER_IP)
    await throttle.ensure_not_locked(throttle.LOGIN, EMAIL, VICTIM_IP)


@pytest.mark.asyncio
async def test_scopes_are_independent(mongo):
    """Failing the tenant login must not lock the platform admin sign-in."""
    await fail_n(settings.LOGIN_MAX_ATTEMPTS + 1, scope=throttle.LOGIN)
    await throttle.ensure_not_locked(throttle.PLATFORM_LOGIN, EMAIL, ATTACKER_IP)


@pytest.mark.asyncio
async def test_success_clears_the_counter(mongo):
    await fail_n(settings.LOGIN_MAX_ATTEMPTS - 1)
    await throttle.clear(throttle.LOGIN, EMAIL, ATTACKER_IP)
    await fail_n(settings.LOGIN_MAX_ATTEMPTS - 1)
    # Still under the limit, because the successful sign-in reset the count.
    await throttle.ensure_not_locked(throttle.LOGIN, EMAIL, ATTACKER_IP)


@pytest.mark.asyncio
async def test_lockout_lifts_once_it_has_expired(mongo):
    """A lock is a delay, not a ban.

    Regression: the remaining-time helper clamped to a minimum of one second, so
    an expired `locked_until` still read as "1 second left" and the account was
    locked out permanently.
    """
    from datetime import timedelta

    from app.core.utils import utcnow

    await fail_n(settings.LOGIN_MAX_ATTEMPTS)
    with pytest.raises(TooManyRequests):
        await throttle.ensure_not_locked(throttle.LOGIN, EMAIL, ATTACKER_IP)

    await mongo[throttle.COLLECTION].update_one(
        {}, {"$set": {"locked_until": utcnow() - timedelta(seconds=1)}}
    )
    await throttle.ensure_not_locked(throttle.LOGIN, EMAIL, ATTACKER_IP)


@pytest.mark.asyncio
async def test_counter_row_carries_its_own_expiry(mongo):
    """The TTL index is what stops this collection growing without bound."""
    await fail_n(1)
    row = await mongo[throttle.COLLECTION].find_one({})
    assert row["expires_at"] is not None
    assert row["failures"] == 1
    assert row["subject"] == EMAIL


@pytest.mark.asyncio
async def test_email_is_normalised(mongo):
    """Mixed case and stray spaces must not open a fresh quota."""
    await fail_n(settings.LOGIN_MAX_ATTEMPTS, email="  SARAH@JenkinsLaw.com ")
    with pytest.raises(TooManyRequests):
        await throttle.ensure_not_locked(throttle.LOGIN, EMAIL, ATTACKER_IP)

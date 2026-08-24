"""Keeps a free-tier instance from being put to sleep.

Render's free plan stops a web service after 15 minutes with no inbound
request, and the next caller waits out a cold start - container pull, Python
import, Atlas connect - before getting an answer. Nothing is broken, but a
first request that takes most of a minute reads as broken, and on a phone it
usually reads as a failed login.

What this does is ask itself for `/ping` on a timer. The request leaves the
container, crosses the public internet and arrives back through Render's proxy,
which is what makes it count as inbound traffic and resets the idle clock.

What this cannot do, and it is the important half: **wake a service that has
already gone to sleep.** The process doing the pinging is the process that was
stopped. So this holds a running instance up; it cannot bring a stopped one
back. That needs a pinger that is somewhere else - see
`.github/workflows/keep-awake.yml`, which is the other half of the same job and
the half that can actually resurrect.

Two limits worth knowing before switching this on:

  * A free Render account gets 750 instance-hours a month, shared across every
    free service on it. A month is about 730 hours, so keeping **one** service
    awake around the clock fits, with very little to spare. Keeping two awake
    does not - both get suspended until the month rolls over.
  * Render's own health checks do not count as traffic, which is why
    `healthCheckPath` in render.yaml does not already solve this.

Off unless there is a URL to ping, so a local run and a test suite never start
a background loop that talks to the internet.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional, Set
from urllib.parse import urlparse

import httpx

from app.core.config import settings

logger = logging.getLogger("app.keepalive")

#: Render sets this itself on every service, so the common case needs no
#: configuration at all - which matters, because a keep-alive pointed at the
#: wrong host is a keep-alive that silently does nothing.
RENDER_URL_VAR = "RENDER_EXTERNAL_URL"

#: Hosts that are never worth pinging: a loopback ping proves nothing and would
#: just spin a timer during development and in CI.
LOCAL_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1", ""}

_task: Optional[asyncio.Task] = None
_pending: Set[asyncio.Task] = set()


def target_url() -> Optional[str]:
    """Where to ping, or None if there is nothing sensible to ping.

    An explicit setting wins, so a deployment somewhere other than Render can
    still use this; otherwise Render's own variable is used.
    """
    configured = settings.KEEPALIVE_URL.strip() or os.getenv(RENDER_URL_VAR, "").strip()
    if not configured:
        return None
    parsed = urlparse(configured if "://" in configured else f"https://{configured}")
    if parsed.hostname in LOCAL_HOSTS or parsed.hostname is None:
        return None
    base = f"{parsed.scheme}://{parsed.netloc}"
    return f"{base}/ping"


def start() -> None:
    """Begins pinging, if this deployment is one where that makes sense.

    Called from the app's lifespan. Never raises: a keep-alive that cannot start
    is a service that sleeps, not a service that fails to boot.
    """
    global _task
    if not settings.KEEPALIVE_ENABLED:
        return
    if _task is not None and not _task.done():
        return
    url = target_url()
    if url is None:
        logger.info(
            "Keep-alive is off: no external URL to ping. On Render this comes "
            "from %s automatically; elsewhere set KEEPALIVE_URL.", RENDER_URL_VAR,
        )
        return
    interval = max(1, settings.KEEPALIVE_INTERVAL_MINUTES)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    _task = loop.create_task(_loop(url, interval * 60))
    _pending.add(_task)
    _task.add_done_callback(_pending.discard)
    logger.info("Keep-alive: pinging %s every %d minute(s)", url, interval)


async def _loop(url: str, interval_seconds: int) -> None:
    """Pings on a timer until the process shuts down.

    The first sleep comes before the first ping on purpose: the boot that started
    this was itself inbound traffic, so pinging immediately would be one request
    that proves nothing.
    """
    # Short timeouts. A ping is not worth holding a connection open for, and a
    # request still in flight when the next tick arrives is a request to drop.
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(10.0, connect=5.0),
        follow_redirects=True,
        headers={"User-Agent": "webimove-keepalive"},
    ) as client:
        while True:
            try:
                await asyncio.sleep(interval_seconds)
            except asyncio.CancelledError:
                raise
            try:
                response = await client.get(url)
                logger.debug("Keep-alive ping: %s", response.status_code)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                # A failed ping is the normal case during a deploy, when the old
                # container is draining and the new one is not up yet. Not worth
                # a warning on every occurrence.
                logger.debug("Keep-alive ping failed: %s", exc)


async def stop() -> None:
    """Stops pinging, so a reload does not leave a timer behind."""
    global _task
    if _task is None:
        return
    _task.cancel()
    try:
        await _task
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        pass
    _task = None


def is_running() -> bool:
    return _task is not None and not _task.done()

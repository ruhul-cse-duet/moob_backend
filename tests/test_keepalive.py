"""Keeping a free-tier instance awake.

The failure mode this guards against is the quiet one: a keep-alive that runs
happily while pinging the wrong thing. A loop that pings `localhost` from inside
the container, or a URL with a stray trailing slash producing `//ping`, looks
identical in the logs to one that works - a task started, no errors - and the
service goes to sleep anyway.

So the URL resolution is what is pinned here, along with the two states that
have to stay no-ops: no URL configured, and a local development run.
"""
import pytest

from app.services import keepalive


@pytest.fixture(autouse=True)
def clear_env(monkeypatch):
    """Render's variable must not leak in from a real environment."""
    monkeypatch.delenv(keepalive.RENDER_URL_VAR, raising=False)
    monkeypatch.setattr(keepalive.settings, "KEEPALIVE_URL", "", raising=False)
    monkeypatch.setattr(keepalive.settings, "KEEPALIVE_ENABLED", True, raising=False)


def test_renders_own_variable_is_used_without_configuration(monkeypatch):
    """The whole point of reading RENDER_EXTERNAL_URL: nothing to fill in.

    A keep-alive that needed the URL pasted in by hand is a keep-alive that is
    wrong on the first deploy from a new blueprint.
    """
    monkeypatch.setenv(keepalive.RENDER_URL_VAR, "https://moob.onrender.com")

    assert keepalive.target_url() == "https://moob.onrender.com/ping"


def test_a_trailing_slash_does_not_produce_a_double_slash(monkeypatch):
    """`//ping` is a different path, and Render answers it with a 404.

    A 404 still counts as inbound traffic, so this would *appear* to work - the
    idle clock really does reset. It is worth pinning anyway: a permanent 404 in
    the logs is the kind of thing that gets "fixed" by turning the keep-alive
    off.
    """
    monkeypatch.setenv(keepalive.RENDER_URL_VAR, "https://moob.onrender.com/")

    assert keepalive.target_url() == "https://moob.onrender.com/ping"


def test_a_bare_hostname_is_assumed_to_be_https(monkeypatch):
    """Someone will paste the host without a scheme. urlparse would otherwise
    read the whole thing as a path and produce a URL with no host at all."""
    monkeypatch.setenv(keepalive.RENDER_URL_VAR, "moob.onrender.com")

    assert keepalive.target_url() == "https://moob.onrender.com/ping"


def test_an_explicit_setting_wins_over_renders(monkeypatch):
    """So a deployment that is not Render can still use this."""
    monkeypatch.setenv(keepalive.RENDER_URL_VAR, "https://render.example.com")
    monkeypatch.setattr(keepalive.settings, "KEEPALIVE_URL",
                        "https://api.webimove.com", raising=False)

    assert keepalive.target_url() == "https://api.webimove.com/ping"


@pytest.mark.parametrize("url", [
    "http://localhost:8002",
    "http://127.0.0.1:8000",
    "http://0.0.0.0:8000",
])
def test_a_local_url_is_never_pinged(monkeypatch, url):
    """A loopback ping proves nothing and resets no idle clock.

    Without this, every `uvicorn --reload` and every test run would start a
    background loop making HTTP requests to itself.
    """
    monkeypatch.setattr(keepalive.settings, "KEEPALIVE_URL", url, raising=False)

    assert keepalive.target_url() is None


def test_no_url_means_no_url(monkeypatch):
    """The state a local run and CI are in, and the one `start` must tolerate."""
    assert keepalive.target_url() is None


def test_start_does_nothing_without_a_url():
    """Not an error. A service with nowhere to ping is a service that sleeps,
    which is the correct behaviour for a local run - not a failed boot."""
    keepalive.start()

    assert keepalive.is_running() is False


def test_start_does_nothing_when_switched_off(monkeypatch):
    """The escape hatch for the 750-hour quota.

    Two free services both holding themselves awake is 1460 instance-hours
    against a 750 allowance, and Render suspends both. Turning one off has to
    actually turn it off.
    """
    monkeypatch.setenv(keepalive.RENDER_URL_VAR, "https://moob.onrender.com")
    monkeypatch.setattr(keepalive.settings, "KEEPALIVE_ENABLED", False, raising=False)

    keepalive.start()

    assert keepalive.is_running() is False


async def test_the_loop_starts_and_stops_cleanly(monkeypatch):
    """Started on boot, cancelled on shutdown, with nothing left behind.

    A task that survives shutdown keeps a reload's old process alive and holds
    its connection pool open.
    """
    monkeypatch.setenv(keepalive.RENDER_URL_VAR, "https://moob.onrender.com")
    monkeypatch.setattr(keepalive.settings, "KEEPALIVE_INTERVAL_MINUTES", 1,
                        raising=False)

    keepalive.start()
    assert keepalive.is_running() is True

    # Idempotent: a second call must not leave two loops pinging.
    keepalive.start()
    first = keepalive._task
    keepalive.start()
    assert keepalive._task is first

    await keepalive.stop()
    assert keepalive.is_running() is False


async def test_the_first_ping_waits_for_the_interval(monkeypatch):
    """The boot that started the loop was itself inbound traffic.

    Pinging immediately would spend a request proving something the boot already
    proved. Asserted because the ordering of sleep and ping inside the loop is
    easy to swap and impossible to notice.
    """
    monkeypatch.setenv(keepalive.RENDER_URL_VAR, "https://moob.onrender.com")
    monkeypatch.setattr(keepalive.settings, "KEEPALIVE_INTERVAL_MINUTES", 60,
                        raising=False)

    pinged = []

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        async def get(self, url):
            pinged.append(url)
            raise AssertionError("should not be reached before the first sleep")

    monkeypatch.setattr(keepalive.httpx, "AsyncClient", lambda **_: FakeClient())

    keepalive.start()
    # Long enough for the loop to reach its first await; the interval is an hour,
    # so nothing should have been pinged.
    import asyncio
    await asyncio.sleep(0.05)

    assert pinged == []
    await keepalive.stop()

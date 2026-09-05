"""Push notification delivery.

What is worth pinning here is the part that fails silently. A push that never
arrives looks exactly like a push nobody sent: there is no error to read, no
row that failed to write, and the phone that should have rung is somebody
else's. So these tests hold the three things that would go unnoticed.

  * With no credentials, nothing is sent and nothing raises. This is the
    shipped default, and the whole feature has to be inert in it.
  * The FCM v1 envelope. A payload with a non-string in `data`, or a missing
    `channel_id`, is accepted by the API and then dropped on the device - so the
    shape is asserted rather than assumed.
  * A dead token is removed. Without this the collection grows a row per
    uninstall forever and every send pays for them.

FCM itself is stubbed. These tests prove the request this code builds, not that
Google's servers accept it - that is what PUSH_DRY_RUN is for against the real
endpoint.
"""
import json

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.services import push


@pytest.fixture
def service_account():
    """A real RSA key, because the assertion is genuinely signed.

    Generated per run rather than checked in: a private key in a repository is
    a private key that ends up somewhere else, even a throwaway one.
    """
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    return {
        "type": "service_account",
        "project_id": "webimove-test",
        "client_email": "push@webimove-test.iam.gserviceaccount.com",
        "private_key": pem,
    }


@pytest.fixture(autouse=True)
def reset_module_state():
    """Every cache in the module, cleared around each test.

    `push` deliberately caches its credentials and access token for the life of
    the process; without this a test would inherit the previous one's.
    """
    def clear():
        push._credentials = None
        push._credentials_loaded = False
        push._token = None
        push._token_expires_at = 0.0
        push._client = None

    clear()
    yield
    clear()


def configure(monkeypatch, account=None, **overrides):
    """Points the module's settings at a test configuration."""
    values = {
        "PUSH_ENABLED": True,
        "PUSH_DRY_RUN": False,
        "FCM_CREDENTIALS_JSON": json.dumps(account) if account else "",
        "FCM_PROJECT_ID": "",
        "FCM_CLIENT_EMAIL": "",
        "FCM_PRIVATE_KEY": "",
        "PUSH_ANDROID_CHANNEL_ID": "webimove_default",
        "PUSH_SOUND": "default",
        "PUSH_TTL_SECONDS": 604800,
        "APNS_BUNDLE_ID": "",
        "PUSH_MAX_CONCURRENCY": 4,
        "FRONTEND_URL": "https://app.webimove.test",
        **overrides,
    }
    for name, value in values.items():
        monkeypatch.setattr(push.settings, name, value, raising=False)


class FakeDevices:
    """Stands in for the `device_tokens` collection.

    Only the three operations `send_to_users` performs: find by user, delete
    many by token, and nothing else. A fake rather than a live Mongo because
    what is under test is the FCM request, not the driver.
    """

    def __init__(self, rows):
        self.rows = list(rows)
        self.deleted = []

    def find(self, query):
        wanted = set(query["user_id"]["$in"])
        rows = [r for r in self.rows if r["user_id"] in wanted
                and r.get("active", True) is not False]

        class Cursor:
            def __aiter__(self):
                async def gen():
                    for row in rows:
                        yield row
                return gen()

        return Cursor()

    async def delete_many(self, query):
        tokens = set(query["token"]["$in"])
        self.deleted.extend(sorted(tokens))
        self.rows = [r for r in self.rows if r["token"] not in tokens]

        class Result:
            deleted_count = len(tokens)

        return Result()


class FakePrefs:
    """Preferences. Empty means everybody is opted in, which is the default."""

    def __init__(self, rows=()):
        self.rows = list(rows)

    def find(self, query):
        wanted = set(query["user_id"]["$in"])
        rows = [r for r in self.rows if r["user_id"] in wanted]

        class Cursor:
            def __aiter__(self):
                async def gen():
                    for row in rows:
                        yield row
                return gen()

        return Cursor()


def stub_fcm(monkeypatch, handler):
    """Routes the module's HTTP client at an in-process transport.

    The token exchange is answered too, so the service-account assertion is
    really signed and really parsed on the way through.
    """
    sent = []

    def route(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "test-access-token",
                                             "expires_in": 3600})
        sent.append(json.loads(request.content))
        return handler(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(route))
    monkeypatch.setattr(push, "_http", lambda: client)
    return sent


# --------------------------------------------------------------------------- #


async def test_unconfigured_is_inert(monkeypatch):
    """The shipped default: no credentials, so nothing is sent and nothing fails.

    The device count still comes back, because "we know about your phone but
    cannot reach it" is a configuration state the app is entitled to see.
    """
    configure(monkeypatch, account=None)
    devices = FakeDevices([{"user_id": "u1", "token": "t1", "platform": "android"}])
    monkeypatch.setattr(push, "_devices", lambda: devices)
    monkeypatch.setattr(push, "_prefs", lambda: FakePrefs())

    result = await push.send_to_users(["u1"], title="Hello", body="World")

    assert push.is_configured() is False
    assert result["devices"] == 1
    assert result["sent"] == 0
    assert result["failed"] == 0  # not a failure - there was nothing to try


async def test_envelope_is_a_valid_v1_message(monkeypatch, service_account):
    """The exact request FCM receives.

    Everything asserted here is silently dropped rather than rejected if it is
    wrong, which is why it is asserted at all.
    """
    configure(monkeypatch, account=service_account)
    devices = FakeDevices([{"user_id": "u1", "token": "token-1", "platform": "android"}])
    monkeypatch.setattr(push, "_devices", lambda: devices)
    monkeypatch.setattr(push, "_prefs", lambda: FakePrefs())
    sent = stub_fcm(monkeypatch, lambda r: httpx.Response(200, json={"name": "ok"}))

    result = await push.send_to_users(
        ["u1"],
        title="Document approved",
        body="Your passport was approved.",
        data={"case_id": "abc", "count": 3, "flag": True, "nothing": None},
        type="document_approved",
        badges={"u1": 7},
        collapse_key="case:abc",
    )

    assert result["sent"] == 1
    message = sent[0]["message"]
    assert message["token"] == "token-1"
    assert message["notification"] == {"title": "Document approved",
                                       "body": "Your passport was approved."}

    # v1 rejects a data payload that is not strings all the way down, and a
    # None would be sent as the literal "None" if it were not dropped.
    assert all(isinstance(v, str) for v in message["data"].values())
    assert message["data"]["count"] == "3"
    assert message["data"]["flag"] == "true"
    assert "nothing" not in message["data"]
    # The type travels in the payload so the app can route the tap.
    assert message["data"]["type"] == "document_approved"
    assert message["data"]["case_id"] == "abc"

    # Android: without the channel id there is no heads-up alert, and without
    # the click action the tap opens the launcher instead of the app's handler.
    android = message["android"]["notification"]
    assert android["channel_id"] == "webimove_default"
    assert android["click_action"] == "FLUTTER_NOTIFICATION_CLICK"
    assert message["android"]["collapse_key"] == "case:abc"

    aps = message["apns"]["payload"]["aps"]
    assert aps["badge"] == 7
    assert aps["alert"]["title"] == "Document approved"
    assert message["apns"]["headers"]["apns-collapse-id"] == "case:abc"

    assert message["webpush"]["fcm_options"]["link"].startswith(
        "https://app.webimove.test")


async def test_dead_token_is_dropped(monkeypatch, service_account):
    """An uninstalled app's token is removed, not retried forever.

    A token FCM has disowned will never work again. Left in place it is a row
    every future send pays to look up and fail on, so the send that discovers it
    is the send that removes it.
    """
    configure(monkeypatch, account=service_account)
    devices = FakeDevices([
        {"user_id": "u1", "token": "live", "platform": "android"},
        {"user_id": "u1", "token": "dead", "platform": "ios"},
    ])
    monkeypatch.setattr(push, "_devices", lambda: devices)
    monkeypatch.setattr(push, "_prefs", lambda: FakePrefs())

    def handler(request: httpx.Request) -> httpx.Response:
        if json.loads(request.content)["message"]["token"] == "dead":
            return httpx.Response(404, json={
                "error": {
                    "status": "NOT_FOUND",
                    "message": "Requested entity was not found.",
                    "details": [{"errorCode": "UNREGISTERED"}],
                }
            })
        return httpx.Response(200, json={"name": "ok"})

    stub_fcm(monkeypatch, handler)

    result = await push.send_to_users(["u1"], title="Hi", body="there")

    assert result["sent"] == 1
    assert result["dropped"] == 1
    # A dead token is not counted as a failure: nothing went wrong, a device
    # went away.
    assert result["failed"] == 0
    assert devices.deleted == ["dead"]


async def test_a_muted_account_is_skipped(monkeypatch, service_account):
    """Muting is enforced before a single device is looked up.

    The notification is still recorded - muting silences the phone, it does not
    hide the update - so this only asserts that nothing was sent.
    """
    configure(monkeypatch, account=service_account)
    devices = FakeDevices([
        {"user_id": "muted", "token": "a", "platform": "android"},
        {"user_id": "typed", "token": "b", "platform": "android"},
        {"user_id": "open", "token": "c", "platform": "android"},
    ])
    monkeypatch.setattr(push, "_devices", lambda: devices)
    monkeypatch.setattr(push, "_prefs", lambda: FakePrefs([
        {"user_id": "muted", "enabled": False},
        {"user_id": "typed", "enabled": True, "muted_types": ["task_assigned"]},
    ]))
    sent = stub_fcm(monkeypatch, lambda r: httpx.Response(200, json={"name": "ok"}))

    result = await push.send_to_users(
        ["muted", "typed", "open"], title="Task", body="assigned",
        type="task_assigned")

    assert result["recipients"] == 1
    assert result["sent"] == 1
    assert [m["message"]["token"] for m in sent] == ["c"]


async def test_dispatch_never_raises(monkeypatch, service_account):
    """The fire-and-forget path swallows everything.

    `dispatch` is called from inside request handlers after the notification is
    already stored. If it could raise, an unreachable phone would turn a
    successful document approval into a 500.
    """
    configure(monkeypatch, account=service_account)

    async def explode(*args, **kwargs):
        raise RuntimeError("FCM is on fire")

    monkeypatch.setattr(push, "send_to_users", explode)

    push.dispatch(["u1"], title="Hello", body="World")
    # The task is scheduled, not awaited; draining it here is what proves the
    # exception is caught inside rather than surfacing as a warning at teardown.
    for task in list(push._pending):
        await task


def test_an_escaped_private_key_is_repaired():
    """A PEM out of a .env has lost its newlines.

    Every deployment dashboard flattens a multi-line value, so the key arrives
    with literal backslash-n. Without this the signature step fails with an
    opaque error and push is off with no obvious reason why.
    """
    escaped = "-----BEGIN PRIVATE KEY-----\\nabc\\ndef\\n-----END PRIVATE KEY-----\\n"
    repaired = push._normalise_key(escaped)
    assert "\\n" not in repaired
    assert repaired.startswith("-----BEGIN PRIVATE KEY-----\n")

    # A key that already has real newlines is left exactly as it is.
    real = "-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----\n"
    assert push._normalise_key(real) == real.strip()


def test_a_key_that_is_not_a_pem_turns_push_off(monkeypatch):
    """Better off than signing with a value that cannot be a key.

    Someone will paste the Firebase *Web API key* here; it is the wrong secret
    entirely. Rejecting it at load time puts one line in the boot log instead of
    a signing failure on every notification.
    """
    configure(monkeypatch, account=None, FCM_PROJECT_ID="p",
              FCM_CLIENT_EMAIL="a@b.c", FCM_PRIVATE_KEY="AIzaSyNotAKeyAtAll")
    assert push.credentials() is None


# ── FCM_CREDENTIALS_JSON that does not resolve ─────────────────────────────
#
# It takes either the service account JSON or a path to it, and a path is the
# easy one to get wrong: the file moves, or the deployment never had it. The
# same three fields spelled out separately are usually sitting right there and
# valid, so a stale path is no reason to turn push off.
def test_a_stale_path_falls_back_to_the_separate_fields(monkeypatch, caplog,
                                                        service_account):
    configure(monkeypatch,
              FCM_CREDENTIALS_JSON="moob-firebase-adminsdk-8273fa2885.json",
              FCM_PROJECT_ID=service_account["project_id"],
              FCM_CLIENT_EMAIL=service_account["client_email"],
              FCM_PRIVATE_KEY=service_account["private_key"])

    with caplog.at_level("WARNING"):
        loaded = push._load_credentials()

    assert loaded is not None
    assert loaded.project_id == service_account["project_id"]
    # And it says which variable it ignored, naming the value it was given.
    assert "not a file here" in caplog.text
    assert "moob-firebase-adminsdk" in caplog.text


def test_a_missing_file_is_not_reported_as_a_syntax_error(monkeypatch, caplog):
    """"Not valid JSON" for a filename sends the reader hunting a syntax error
    in a file they never had."""
    configure(monkeypatch, FCM_CREDENTIALS_JSON="does-not-exist.json")

    with caplog.at_level("WARNING"):
        assert push._load_credentials() is None

    assert "not a file here" in caplog.text
    assert "not valid JSON" not in caplog.text


def test_broken_json_still_falls_back(monkeypatch, service_account):
    configure(monkeypatch,
              FCM_CREDENTIALS_JSON="{not json at all",
              FCM_PROJECT_ID=service_account["project_id"],
              FCM_CLIENT_EMAIL=service_account["client_email"],
              FCM_PRIVATE_KEY=service_account["private_key"])

    assert push._load_credentials() is not None


def test_a_usable_json_still_wins_over_the_separate_fields(monkeypatch,
                                                           service_account):
    configure(monkeypatch, account=service_account,
              FCM_PROJECT_ID="ignored-project")

    loaded = push._load_credentials()

    assert loaded is not None
    assert loaded.project_id == service_account["project_id"]

"""Push delivery for the notification feed.

Every notification the API writes already lands in `notifications` (or the
platform-side inboxes) and is announced over the socket. Both need the app to
be *running*: the socket dies with the process, and the feed is only read when
a screen asks. This module is the third leg - the operating system shows the
update even when the app is closed.

Design
------
One choke point, `dispatch`, is called by `events.notify` and therefore covers
every notification in the system without each domain module knowing push
exists. It never raises and never blocks: the work is handed to a background
task, because a phone that is offline must not slow down the request that
happened to notify it.

Transport is FCM HTTP v1 (the legacy server-key API is switched off by Google).
Authentication is a service-account JWT exchanged for an OAuth access token,
signed here with the RSA support `python-jose` already brings in - so there is
no extra dependency and no vendored Google SDK.

Device tokens live in the **platform** database even for tenant users. A
notification's recipients are user ids, and looking them up must not depend on
knowing which of N tenant databases each one is in; user ids are ObjectIds and
unique across the fleet, so one collection answers for everybody, platform
administrators included.

Nothing here is required for the product to work. With no credentials
configured every function is a no-op that still records device registrations,
so filling in the .env later turns push on with no code change.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

import httpx
from jose import jwt

from app.core.config import settings
from app.core.utils import utcnow
from app.db.mongo import platform_db

logger = logging.getLogger("app.push")

FCM_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
FCM_SEND_URL = "https://fcm.googleapis.com/v1/projects/{project}/messages:send"

#: Platforms the app may register as. Anything else is stored as "unknown"
#: rather than rejected - a new Flutter target must not fail registration.
KNOWN_PLATFORMS = {"android", "ios", "web", "macos", "windows", "linux"}

#: FCM's way of saying "this token is dead". Anything on this list means the
#: registration is removed rather than retried: the app was uninstalled, the
#: token was rotated, or it belongs to another Firebase project entirely.
DEAD_TOKEN_CODES = {
    "UNREGISTERED",
    "INVALID_ARGUMENT",
    "SENDER_ID_MISMATCH",
    "NOT_FOUND",
}


# --------------------------------------------------------------------------- #
# Credentials
# --------------------------------------------------------------------------- #


class _Credentials:
    """The service account, however the operator chose to supply it."""

    __slots__ = ("project_id", "client_email", "private_key")

    def __init__(self, project_id: str, client_email: str, private_key: str) -> None:
        self.project_id = project_id
        self.client_email = client_email
        self.private_key = private_key


def _normalise_key(key: str) -> str:
    """A PEM that has been through a .env file has lost its real newlines.

    An escaped newline - the usual shape when the JSON is flattened onto one
    line - and a key wrapped in quotes are both accepted, because both are what
    actually turns up in a deployment dashboard.
    """
    key = key.strip().strip('"').strip("'")
    if "\\n" in key and "\n" not in key:
        key = key.replace("\\n", "\n")
    return key


def _service_account(raw: str) -> Dict[str, Any]:
    """The service account FCM_CREDENTIALS_JSON names, or an empty mapping.

    A path is far easier to get right than a pasted multi-line JSON, so both
    are accepted and the shape decides which one this is. A value that is
    neither says so in those words: reporting "not valid JSON" for a filename
    that simply is not there sends whoever reads the log looking for a syntax
    error in a file they never had.

    Returning empty rather than giving up lets FCM_PROJECT_ID and its two
    companions still be used. They are the same three fields, spelled out, and
    a stale path in one variable is no reason to ignore credentials that are
    sitting right there and valid.
    """
    if not raw.startswith("{"):
        if not os.path.isfile(raw):
            logger.warning(
                "FCM_CREDENTIALS_JSON names %r, which is not a file here and is "
                "not JSON either. Falling back to FCM_PROJECT_ID, "
                "FCM_CLIENT_EMAIL and FCM_PRIVATE_KEY.", raw[:120])
            return {}
        try:
            with open(raw, "r", encoding="utf-8") as handle:
                raw = handle.read()
        except OSError as exc:
            logger.warning("FCM_CREDENTIALS_JSON points at a file that cannot be "
                           "read (%s); falling back to the separate fields", exc)
            return {}
    try:
        data = json.loads(raw)
    except ValueError as exc:
        logger.warning("FCM_CREDENTIALS_JSON is not valid JSON (%s); falling back "
                       "to the separate fields", exc)
        return {}
    if not isinstance(data, dict):
        logger.warning("FCM_CREDENTIALS_JSON is not a service account object; "
                       "falling back to the separate fields")
        return {}
    return data


def _load_credentials() -> Optional[_Credentials]:
    """Reads the service account, or returns None if push is not configured."""
    raw = settings.FCM_CREDENTIALS_JSON.strip()
    data = _service_account(raw) if raw else {}
    project = data.get("project_id") or settings.FCM_PROJECT_ID
    email = data.get("client_email") or settings.FCM_CLIENT_EMAIL
    key = data.get("private_key") or settings.FCM_PRIVATE_KEY

    project, email, key = project.strip(), email.strip(), _normalise_key(key)
    if not (project and email and key):
        return None
    if "PRIVATE KEY" not in key:
        logger.error("The FCM private key does not look like a PEM block; push is off")
        return None
    return _Credentials(project, email, key)


_credentials: Optional[_Credentials] = None
_credentials_loaded = False


def credentials() -> Optional[_Credentials]:
    """Cached: the .env does not change while the process is running."""
    global _credentials, _credentials_loaded
    if not _credentials_loaded:
        _credentials = _load_credentials() if settings.PUSH_ENABLED else None
        _credentials_loaded = True
        if settings.PUSH_ENABLED and _credentials is None:
            logger.info("Push notifications: no FCM credentials configured - "
                        "devices are still registered, nothing is sent")
        elif _credentials is not None:
            logger.info("Push notifications: FCM project %s%s",
                        _credentials.project_id,
                        " (dry run)" if settings.PUSH_DRY_RUN else "")
    return _credentials


def is_configured() -> bool:
    return credentials() is not None


# --------------------------------------------------------------------------- #
# Access tokens
# --------------------------------------------------------------------------- #

_token: Optional[str] = None
_token_expires_at: float = 0.0
_token_lock: Optional[asyncio.Lock] = None
_client: Optional[httpx.AsyncClient] = None


def _http() -> httpx.AsyncClient:
    """One connection pool for the process - FCM is a per-device fanout."""
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=10.0))
    return _client


async def close() -> None:
    """Called from the app's shutdown so the pool is not left dangling."""
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


async def _access_token() -> Optional[str]:
    """A bearer token for the FCM API, minted from the service account.

    Cached until a minute before it expires. The lock matters: a broadcast
    starts dozens of sends at once, and without it every one of them would mint
    its own token on a cold cache.
    """
    global _token, _token_expires_at, _token_lock
    creds = credentials()
    if creds is None:
        return None
    if _token and time.time() < _token_expires_at:
        return _token
    if _token_lock is None:
        _token_lock = asyncio.Lock()
    async with _token_lock:
        # Another waiter may have refreshed it while this one queued.
        if _token and time.time() < _token_expires_at:
            return _token
        now = int(time.time())
        try:
            assertion = jwt.encode(
                {
                    "iss": creds.client_email,
                    "sub": creds.client_email,
                    "scope": FCM_SCOPE,
                    "aud": GOOGLE_TOKEN_URL,
                    "iat": now,
                    "exp": now + 3600,
                },
                creds.private_key,
                algorithm="RS256",
            )
        except Exception as exc:  # noqa: BLE001 - a bad key must not crash a request
            logger.error("Could not sign the FCM service-account assertion: %s", exc)
            return None
        try:
            response = await _http().post(
                GOOGLE_TOKEN_URL,
                data={
                    "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                    "assertion": assertion,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        except httpx.HTTPError as exc:
            logger.warning("Could not reach Google to exchange the FCM assertion: %s", exc)
            return None
        if response.status_code != 200:
            logger.error("Google refused the FCM assertion (%s): %s",
                         response.status_code, response.text[:300])
            return None
        payload = response.json()
        _token = payload.get("access_token")
        _token_expires_at = time.time() + max(60, int(payload.get("expires_in", 3600)) - 60)
        return _token


# --------------------------------------------------------------------------- #
# Device registry
# --------------------------------------------------------------------------- #


def _devices():
    return platform_db().device_tokens


def _prefs():
    return platform_db().push_preferences


async def register_device(
    *,
    user_id: str,
    token: str,
    platform: str = "unknown",
    device_id: Optional[str] = None,
    device_name: Optional[str] = None,
    app_version: Optional[str] = None,
    locale: Optional[str] = None,
    tenant_id: Optional[str] = None,
    role: Optional[str] = None,
) -> Dict[str, Any]:
    """Records one device for one account.

    Keyed on the token, not on the account: the same handset handed to a second
    person keeps its token, and this is what moves it across so the previous
    owner stops receiving that account's notifications.
    """
    token = (token or "").strip()
    if not token:
        raise ValueError("A device token is required")
    platform = (platform or "unknown").strip().lower()
    if platform not in KNOWN_PLATFORMS:
        platform = "unknown"
    now = utcnow()
    await _devices().update_one(
        {"token": token},
        {
            "$set": {
                "user_id": user_id,
                "tenant_id": tenant_id,
                "role": role,
                "platform": platform,
                "device_id": device_id,
                "device_name": device_name,
                "app_version": app_version,
                "locale": locale,
                "active": True,
                "updated_at": now,
                "last_seen_at": now,
            },
            "$setOnInsert": {"token": token, "created_at": now},
        },
        upsert=True,
    )
    return await _devices().find_one({"token": token}) or {}


async def unregister_device(*, user_id: str, token: str) -> bool:
    """Forgets one device.

    Scoped to the account so one person cannot unregister another's handset by
    guessing a token.
    """
    result = await _devices().delete_one({"token": (token or "").strip(),
                                          "user_id": user_id})
    return bool(result.deleted_count)


async def unregister_all(*, user_id: str) -> int:
    result = await _devices().delete_many({"user_id": user_id})
    return int(result.deleted_count)


async def list_devices(*, user_id: str) -> List[Dict[str, Any]]:
    return [d async for d in
            _devices().find({"user_id": user_id}).sort("last_seen_at", -1)]


async def _drop_tokens(tokens: Iterable[str]) -> None:
    doomed = [t for t in tokens if t]
    if not doomed:
        return
    await _devices().delete_many({"token": {"$in": doomed}})
    logger.info("Dropped %d dead device token(s)", len(doomed))


# --------------------------------------------------------------------------- #
# Preferences
# --------------------------------------------------------------------------- #


async def get_preferences(user_id: str) -> Dict[str, Any]:
    doc = await _prefs().find_one({"user_id": user_id}) or {}
    return {
        "enabled": bool(doc.get("enabled", True)),
        "muted_types": list(doc.get("muted_types") or []),
    }


async def set_preferences(user_id: str, *, enabled: Optional[bool] = None,
                          muted_types: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    now = utcnow()
    update: Dict[str, Any] = {"updated_at": now}
    if enabled is not None:
        update["enabled"] = bool(enabled)
    if muted_types is not None:
        update["muted_types"] = sorted({str(t) for t in muted_types if t})
    await _prefs().update_one(
        {"user_id": user_id},
        {"$set": update, "$setOnInsert": {"user_id": user_id, "created_at": now}},
        upsert=True,
    )
    return await get_preferences(user_id)


async def _allowed_recipients(user_ids: Sequence[str], type: str) -> Set[str]:
    """Removes the accounts that have muted this kind of update.

    Only the accounts with a stored preference are read; everyone else is
    opted in, which is what makes push work the moment credentials land without
    anybody having to visit a settings screen first.
    """
    wanted = {u for u in user_ids if u}
    if not wanted:
        return set()
    async for doc in _prefs().find({"user_id": {"$in": list(wanted)}}):
        if not doc.get("enabled", True) or type in (doc.get("muted_types") or []):
            wanted.discard(doc["user_id"])
    return wanted


# --------------------------------------------------------------------------- #
# Sending
# --------------------------------------------------------------------------- #


def _stringify(data: Optional[Dict[str, Any]]) -> Dict[str, str]:
    """FCM v1 rejects a data payload that is not strings all the way down."""
    out: Dict[str, str] = {}
    for key, value in (data or {}).items():
        if value is None:
            continue
        if isinstance(value, (dict, list)):
            out[str(key)] = json.dumps(value, default=str)
        elif isinstance(value, bool):
            out[str(key)] = "true" if value else "false"
        else:
            out[str(key)] = str(value)
    return out


def _envelope(
    *,
    token: str,
    title: str,
    body: str,
    data: Dict[str, str],
    badge: Optional[int],
    collapse_key: Optional[str],
) -> Dict[str, Any]:
    """One FCM v1 message, shaped per platform.

    The `notification` block is what makes the OS draw the alert while the app
    is not running; `data` is what the app reads to know where to navigate. Both
    are always sent, because a data-only message is quietly dropped on iOS
    unless the app happens to be alive to handle it.
    """
    ttl = max(0, int(settings.PUSH_TTL_SECONDS))
    message: Dict[str, Any] = {
        "token": token,
        "notification": {"title": title, "body": body},
        "data": data,
        "android": {
            "priority": "high",
            "ttl": f"{ttl}s",
            "notification": {
                "channel_id": settings.PUSH_ANDROID_CHANNEL_ID,
                "sound": settings.PUSH_SOUND,
                # Without this the tap opens the launcher rather than handing
                # the payload to Flutter.
                "click_action": "FLUTTER_NOTIFICATION_CLICK",
                "default_vibrate_timings": True,
            },
        },
        "apns": {
            "headers": {
                "apns-priority": "10",
                "apns-push-type": "alert",
                "apns-expiration": str(int(time.time()) + ttl),
            },
            "payload": {
                "aps": {
                    "alert": {"title": title, "body": body},
                    "sound": settings.PUSH_SOUND,
                    "mutable-content": 1,
                }
            },
        },
        "webpush": {
            "headers": {"TTL": str(ttl)},
            "notification": {"title": title, "body": body},
            "fcm_options": {
                "link": f"{settings.FRONTEND_URL.rstrip('/')}/#/notifications"
            },
        },
    }
    if settings.APNS_BUNDLE_ID:
        message["apns"]["headers"]["apns-topic"] = settings.APNS_BUNDLE_ID
    if badge is not None:
        message["apns"]["payload"]["aps"]["badge"] = int(badge)
    if collapse_key:
        # Replaces an earlier unread alert about the same thing instead of
        # stacking five of them in the shade.
        message["android"]["collapse_key"] = collapse_key
        message["apns"]["headers"]["apns-collapse-id"] = collapse_key[:64]
    return message


def _dead_token(payload: Dict[str, Any]) -> bool:
    error = payload.get("error") or {}
    if error.get("status") in DEAD_TOKEN_CODES:
        return True
    for detail in error.get("details") or []:
        if detail.get("errorCode") in DEAD_TOKEN_CODES:
            return True
    # A malformed token reads as a plain 400 about the `token` field.
    return "not a valid FCM registration token" in str(error.get("message") or "")


async def _send_one(url: str, headers: Dict[str, str],
                    message: Dict[str, Any]) -> Tuple[bool, bool]:
    """Sends one message. Returns (delivered, token_is_dead)."""
    payload: Dict[str, Any] = {"message": message}
    if settings.PUSH_DRY_RUN:
        payload["validate_only"] = True
    try:
        response = await _http().post(url, headers=headers, json=payload)
    except httpx.HTTPError as exc:
        logger.warning("Push send could not reach FCM: %s", exc)
        return False, False
    if response.status_code == 200:
        return True, False
    try:
        body = response.json()
    except ValueError:
        body = {}
    if response.status_code in {400, 403, 404} and _dead_token(body):
        return False, True
    logger.warning("FCM refused a message (%s): %s",
                   response.status_code, response.text[:300])
    return False, False


async def send_to_users(
    user_ids: Sequence[str],
    *,
    title: str,
    body: str = "",
    data: Optional[Dict[str, Any]] = None,
    type: str = "system",
    badges: Optional[Dict[str, int]] = None,
    collapse_key: Optional[str] = None,
) -> Dict[str, int]:
    """Pushes one update to every device of every named account.

    Returns a tally rather than raising, so a caller that wants to report on
    delivery can, and one that does not can ignore it.
    """
    result = {"recipients": 0, "devices": 0, "sent": 0, "failed": 0, "dropped": 0}
    if not settings.PUSH_ENABLED:
        return result
    recipients = await _allowed_recipients(list(user_ids), type)
    result["recipients"] = len(recipients)
    if not recipients:
        return result

    devices = [d async for d in _devices().find(
        {"user_id": {"$in": list(recipients)}, "active": {"$ne": False}})]
    result["devices"] = len(devices)
    if not devices:
        return result

    creds = credentials()
    if creds is None:
        # Registered devices but no credentials: a configuration state, not a
        # failure, so nothing is counted as failed.
        return result
    access_token = await _access_token()
    if not access_token:
        result["failed"] = len(devices)
        return result

    url = FCM_SEND_URL.format(project=creds.project_id)
    headers = {"Authorization": f"Bearer {access_token}",
               "Content-Type": "application/json; UTF-8"}
    payload_data = _stringify({**(data or {}), "type": type,
                               "title": title, "body": body})

    semaphore = asyncio.Semaphore(max(1, settings.PUSH_MAX_CONCURRENCY))
    dead: List[str] = []

    async def _one(device: Dict[str, Any]) -> None:
        async with semaphore:
            delivered, is_dead = await _send_one(url, headers, _envelope(
                token=device["token"],
                title=title,
                body=body,
                data=payload_data,
                badge=(badges or {}).get(device.get("user_id")),
                collapse_key=collapse_key,
            ))
            if delivered:
                result["sent"] += 1
            else:
                result["failed"] += 1
            if is_dead:
                dead.append(device["token"])

    await asyncio.gather(*(_one(d) for d in devices))
    if dead:
        await _drop_tokens(dead)
        result["dropped"] = len(dead)
        result["failed"] -= len(dead)
    return result


# --------------------------------------------------------------------------- #
# Fire and forget
# --------------------------------------------------------------------------- #

#: Tasks are held here for their lifetime. Without a strong reference the event
#: loop is free to garbage-collect a running task, and the push vanishes.
_pending: Set[asyncio.Task] = set()


def dispatch(
    user_ids: Sequence[str],
    *,
    title: str,
    body: str = "",
    data: Optional[Dict[str, Any]] = None,
    type: str = "system",
    badges: Optional[Dict[str, int]] = None,
    collapse_key: Optional[str] = None,
) -> None:
    """Sends in the background. Never raises, never delays the caller.

    This is what `events.notify` calls, which is why no domain module has to
    know push exists. A notification is already stored by the time this runs, so
    losing one to a network failure costs the alert, never the record.
    """
    if not settings.PUSH_ENABLED:
        return
    ids = [u for u in user_ids if u]
    if not ids:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No running loop (a script, a sync test) - nothing to schedule onto.
        return
    task = loop.create_task(_guarded(
        ids, title=title, body=body, data=data, type=type,
        badges=badges, collapse_key=collapse_key,
    ))
    _pending.add(task)
    task.add_done_callback(_pending.discard)


async def _guarded(user_ids: Sequence[str], **kwargs: Any) -> None:
    try:
        await send_to_users(user_ids, **kwargs)
    except Exception as exc:  # noqa: BLE001 - a background task must not die loudly
        logger.warning("Push notification could not be delivered: %s", exc)
        logger.debug("Push failure", exc_info=True)

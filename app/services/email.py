"""Outgoing mail, over SMTP or over an HTTP API.

Why there are two transports
----------------------------
Render blocks outbound connections on ports 25, 465 and 587 to keep its
platform from being used to send spam. Gmail offers SMTP on 465 and 587 and
nothing else, so `smtp.gmail.com` cannot be reached from a Render service at
all - the TCP handshake never completes and aiosmtplib eventually reports
`SMTPConnectTimeoutError`. It reads like a credentials problem and is not one:
a *connect* timeout rather than an auth failure is the tell that the packets
were dropped by a firewall rather than answered by a mail server.

An HTTP API sends over 443, which nothing blocks. So the transport is chosen
from what is configured: set one provider's API key and mail goes over HTTPS;
set none and it falls back to SMTP, which is still the right thing locally and
on a host that permits it.

Nothing here raises. Mail is a side effect of a request - a signup that cannot
send its verification code should record the account and say so, not return a
500 - so every path returns a bool and logs the reason.
"""
import logging
from email.message import EmailMessage
from typing import Any, Dict, List, Optional, Sequence, Tuple

import aiosmtplib
import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

#: The ports Render (and most PaaS hosts) drop outbound. Kept here so the boot
#: check and the failure message can name the same list.
BLOCKED_SMTP_PORTS = {25, 465, 587}

#: Providers whose free tier is enough to run a product on, in the order they
#: are tried when no explicit provider is named.
_HTTP_PROVIDERS = ("resend", "brevo", "sendgrid")

_client: Optional[httpx.AsyncClient] = None


def _http() -> httpx.AsyncClient:
    """One pool for the process. Mail is bursty - an invite fanout sends several
    in a row - and a fresh TLS handshake per message is pure latency."""
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=10.0))
    return _client


async def close() -> None:
    """Called from the app's shutdown so a reload does not leak a pool."""
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


def _api_key(provider: str) -> str:
    return {
        "resend": settings.RESEND_API_KEY,
        "brevo": settings.BREVO_API_KEY,
        "sendgrid": settings.SENDGRID_API_KEY,
    }.get(provider, "").strip()


def active_provider() -> str:
    """Which transport will actually be used: a provider name, or "smtp".

    Auto-detected from whichever key is filled in, so switching provider is one
    key in the environment rather than a key plus a mode setting to keep in
    sync. An explicit EMAIL_PROVIDER overrides that, for the case where two keys
    are present and only one is meant to be live.
    """
    chosen = settings.EMAIL_PROVIDER.strip().lower()
    if chosen:
        return chosen
    for provider in _HTTP_PROVIDERS:
        if _api_key(provider):
            return provider
    return "smtp"


def describe_transport() -> str:
    """One line for the boot log, so the transport is never a guess."""
    provider = active_provider()
    if provider == "smtp":
        return f"SMTP {settings.SMTP_HOST}:{settings.SMTP_PORT}"
    return f"{provider} HTTP API (from {settings.SMTP_FROM_EMAIL})"


def smtp_is_probably_blocked() -> Optional[str]:
    """Whether this deployment is configured to do something that cannot work.

    Returned rather than logged so the caller decides where it goes. This is the
    check that turns a silent production failure - codes that never arrive, one
    stack trace per attempt buried in the log - into a line at startup.
    """
    if active_provider() != "smtp":
        return None
    if settings.SMTP_PORT not in BLOCKED_SMTP_PORTS:
        return None
    if not settings.is_paas:
        return None
    return (
        f"Email is configured for SMTP on port {settings.SMTP_PORT}, which this "
        f"host blocks outbound - every send will time out. Set RESEND_API_KEY, "
        f"BREVO_API_KEY or SENDGRID_API_KEY to send over HTTPS instead, or move "
        f"to an SMTP provider offering port 2525."
    )


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


async def send_email(
    *,
    to: str | Sequence[str],
    subject: str,
    html: str,
    text: str | None = None,
    reply_to: str | None = None,
    from_name: str | None = None,
) -> bool:
    """Send one message to one or more recipients.

    ``From`` stays on ``SMTP_FROM_EMAIL`` whichever transport is used: SMTP
    authenticates as that mailbox, and an HTTP provider will only accept a
    sender on a domain that has been verified with it. Pass ``reply_to`` (the
    user's own address, say) so a reply reaches them rather than the mailbox.
    """
    recipients = [to] if isinstance(to, str) else [addr for addr in to if addr]
    if not recipients:
        logger.warning("send_email called with no recipients: %s", subject)
        return False

    display_name = from_name or settings.SMTP_FROM_NAME
    body_text = text or "Please view this email in an HTML capable client."
    provider = active_provider()

    if settings.ENVIRONMENT == "test":
        logger.info("Email disabled in test - would send to %s: %s", recipients, subject)
        return False

    if provider == "smtp":
        if not settings.SMTP_HOST:
            logger.info(
                "No email transport configured - would send to %s (reply-to=%s): %s",
                recipients, reply_to, subject,
            )
            return False
        return await _send_smtp(
            recipients=recipients, subject=subject, html=html, text=body_text,
            reply_to=reply_to, display_name=display_name,
        )

    key = _api_key(provider)
    if not key:
        logger.error(
            "EMAIL_PROVIDER is %r but its API key is empty; cannot send %r",
            provider, subject,
        )
        return False

    try:
        url, headers, payload = _build_request(
            provider=provider, key=key, recipients=recipients, subject=subject,
            html=html, text=body_text, reply_to=reply_to, display_name=display_name,
        )
    except ValueError as exc:
        logger.error("Unknown EMAIL_PROVIDER %r: %s", provider, exc)
        return False

    try:
        response = await _http().post(url, headers=headers, json=payload)
    except httpx.HTTPError as exc:
        logger.warning("Could not reach %s to send %r: %s", provider, subject, exc)
        return False

    if response.status_code < 300:
        # INFO because "did it actually go out?" is the first question asked
        # when someone says they never got it, and silence answers nothing.
        logger.info("Sent %r to %s via %s", subject, recipients, provider)
        return True

    # The body is where the real reason lives - an unverified sender domain, a
    # key from the wrong environment - and none of it is secret.
    logger.error("%s refused %r (%s): %s", provider, subject,
                 response.status_code, response.text[:400])
    return False


# --------------------------------------------------------------------------- #
# Transports
# --------------------------------------------------------------------------- #


async def _send_smtp(*, recipients: List[str], subject: str, html: str, text: str,
                     reply_to: Optional[str], display_name: str) -> bool:
    message = EmailMessage()
    message["From"] = f"{display_name} <{settings.SMTP_FROM_EMAIL}>"
    message["To"] = ", ".join(recipients)
    message["Subject"] = subject
    if reply_to:
        message["Reply-To"] = reply_to
    message.set_content(text)
    message.add_alternative(html, subtype="html")

    try:
        await aiosmtplib.send(
            message,
            hostname=settings.SMTP_HOST,
            port=settings.SMTP_PORT,
            username=settings.SMTP_USER or None,
            password=settings.SMTP_PASSWORD or None,
            start_tls=settings.SMTP_STARTTLS,
        )
        logger.info("Sent %r to %s via SMTP", subject, recipients)
        return True
    except aiosmtplib.SMTPConnectTimeoutError:
        # The one failure with a specific, actionable cause. Said plainly
        # instead of as a stack trace, because the trace points at aiosmtplib
        # and the answer has nothing to do with aiosmtplib.
        hint = smtp_is_probably_blocked()
        logger.error(
            "Timed out connecting to %s:%s - %s",
            settings.SMTP_HOST, settings.SMTP_PORT,
            hint or "the mail server did not answer. Check the host and port, "
                    "and whether this network permits outbound SMTP.",
        )
        return False
    except Exception:  # noqa: BLE001 - never let mail failure break a request
        logger.exception("Failed to send email to %s", recipients)
        return False


def _build_request(*, provider: str, key: str, recipients: List[str], subject: str,
                   html: str, text: str, reply_to: Optional[str],
                   display_name: str) -> Tuple[str, Dict[str, str], Dict[str, Any]]:
    """The one request each provider wants. Three shapes, one send path."""
    sender = settings.SMTP_FROM_EMAIL

    if provider == "resend":
        payload: Dict[str, Any] = {
            "from": f"{display_name} <{sender}>",
            "to": recipients,
            "subject": subject,
            "html": html,
            "text": text,
        }
        if reply_to:
            payload["reply_to"] = reply_to
        return ("https://api.resend.com/emails",
                {"Authorization": f"Bearer {key}"}, payload)

    if provider == "brevo":
        payload = {
            "sender": {"email": sender, "name": display_name},
            "to": [{"email": address} for address in recipients],
            "subject": subject,
            "htmlContent": html,
            "textContent": text,
        }
        if reply_to:
            payload["replyTo"] = {"email": reply_to}
        return ("https://api.brevo.com/v3/smtp/email",
                {"api-key": key, "accept": "application/json"}, payload)

    if provider == "sendgrid":
        payload = {
            "personalizations": [{"to": [{"email": a} for a in recipients]}],
            "from": {"email": sender, "name": display_name},
            "subject": subject,
            "content": [
                # SendGrid sends these in order and the client shows the last it
                # can render, so text has to come first or the HTML is ignored.
                {"type": "text/plain", "value": text},
                {"type": "text/html", "value": html},
            ],
        }
        if reply_to:
            payload["reply_to"] = {"email": reply_to}
        return ("https://api.sendgrid.com/v3/mail/send",
                {"Authorization": f"Bearer {key}"}, payload)

    raise ValueError(f"expected one of {', '.join(_HTTP_PROVIDERS)} or smtp")


# --------------------------------------------------------------------------- #
# Templates
# --------------------------------------------------------------------------- #


def _wrap(body: str) -> str:
    return f"""
    <div style="font-family:Inter,Arial,sans-serif;background:#f5f7f8;padding:32px">
  <div style="max-width:520px;margin:auto;background:#fff;border-radius:12px;padding:32px">
    <div style="font-weight:700;letter-spacing:.08em;color:#0f9fa8;margin-bottom:24px">WEB IMOVE</div>
    {body}
    <p style="color:#98a2a6;font-size:12px;margin-top:32px">
      &copy; 2026 WebImove &middot; Secure immigration workspace
    </p>
  </div>
</div>"""


async def send_otp_email(*, to: str, code: str, purpose: str) -> bool:
    titles = {
        "email_verification": "Check your inbox",
        "password_reset": "Reset your password",
        "login": "Your sign-in code",
    }
    body = f"""
    <h2 style="margin:0 0 8px">{titles.get(purpose, 'Your verification code')}</h2>
    <p style="color:#5b686c;margin:0 0 24px">Enter the six-digit code below to continue.</p>
    <div style="font-size:32px;letter-spacing:12px;font-weight:700;color:#0d1b1e">{code}</div>
    <p style="color:#98a2a6;font-size:13px;margin-top:24px">
      Codes expire after {settings.OTP_EXPIRE_MINUTES} minutes to protect your account.
      If you did not request this, ignore this email.
    </p>"""
    return await send_email(
        to=to,
        subject=f"{code} is your WebImove code",
        html=_wrap(body),
        # A code is the one message worth a readable plain-text part: it is what
        # a watch or a notification preview shows.
        text=f"Your WebImove code is {code}. It expires in "
             f"{settings.OTP_EXPIRE_MINUTES} minutes.",
    )


async def send_partner_invite_email(*, to: str, name: str, org: str, role: str, link: str,
                                    invited_by: str | None = None) -> bool:
    who = f"<strong>{invited_by}</strong> at " if invited_by else ""
    body = f"""
    <h2 style="margin:0 0 8px">You have been invited to {org}</h2>
    <p style="color:#5b686c">Hi {name}, {who}{org} has added you as
    <strong>{role}</strong>. Click below to set a password and activate your account.</p>
    <a href="{link}" style="display:inline-block;margin-top:16px;background:#0f9fa8;color:#fff;
       padding:12px 20px;border-radius:8px;text-decoration:none">Set my password</a>
    <p style="color:#98a2a6;font-size:13px;margin-top:24px">
      This link is single-use and expires in {settings.INVITE_EXPIRE_DAYS} days.
      Partners never see billing.
    </p>"""
    return await send_email(
        to=to,
        subject=f"{org} invited you to WebImove",
        html=_wrap(body),
        text=f"{org} added you as {role} on WebImove. Set your password: {link}",
    )


async def send_client_invite_email(*, to: str, name: str, org: str, link: str,
                                   consultant: str | None = None) -> bool:
    body = f"""
    <h2 style="margin:0 0 8px">{org} created your client workspace</h2>
    <p style="color:#5b686c">Hi {name}, {"your consultant " + consultant + " at " if consultant else ""}{org}
    created your workspace. Track your request, upload documents and follow every stage
    from the WebImove app.</p>
    <a href="{link}" style="display:inline-block;margin-top:16px;background:#0f9fa8;color:#fff;
       padding:12px 20px;border-radius:8px;text-decoration:none">Get started</a>"""
    return await send_email(
        to=to,
        subject=f"{org} invited you to WebImove",
        html=_wrap(body),
        text=f"{org} created your WebImove workspace. Get started: {link}",
    )

"""Outgoing mail, over a provider's HTTP API.

Why there is no SMTP transport
------------------------------
Render blocks outbound connections on ports 25, 465 and 587 to keep its
platform from being used to send spam, and Gmail offers SMTP on 465 and 587 and
nothing else - so `smtp.gmail.com` is unreachable from a Render service, full
stop. The TCP handshake never completes; it surfaces as a connect timeout, which
reads like bad credentials and is not. Rather than keep a transport that cannot
work where this runs, mail goes over HTTPS on 443, which nothing blocks.

Brevo is the default. Resend and SendGrid speak the same three-field shape and
are kept as alternatives, so a provider outage or a sending-limit change is one
environment variable rather than a code change.

With no key configured at all, mail is logged instead of sent. That is the local
development path: signup works end to end and the code is in the console.

Nothing here raises. Mail is a side effect of a request - a signup that cannot
send its verification code should record the account and say so, not return a
500 - so every path returns a bool and logs the reason.
"""
import logging
from typing import Any, Dict, List, Optional, Sequence, Tuple

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

#: Tried in this order when no explicit provider is named. Brevo first: it is
#: the default for this deployment, and its free tier needs no verified domain
#: to start sending.
_HTTP_PROVIDERS = ("brevo", "resend", "sendgrid")

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
    """Which provider will be used, or "" when mail is only logged.

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
    return ""


def describe_transport() -> str:
    """One line for the boot log, so the transport is never a guess."""
    provider = active_provider()
    if not provider:
        return "none configured - mail will be logged, not sent"
    if provider not in _HTTP_PROVIDERS:
        return f"UNKNOWN provider {provider!r} - nothing will be sent"
    return f"{provider} HTTP API (from {settings.SMTP_FROM_EMAIL})"


def mail_is_not_configured() -> Optional[str]:
    """Whether this deployment can actually deliver mail.

    Returned rather than logged so the caller decides where it goes. Without it
    the failure is silent in the worst way: signup succeeds, the account exists,
    and the verification code simply never arrives.
    """
    provider = active_provider()

    # An EMAIL_PROVIDER nobody recognises. Reported in every environment, not
    # just production: it is always a mistake, and the symptom is identical to
    # working mail right up until someone waits for a code. `smtp` is the one
    # worth naming - it was a valid value before the SMTP transport was removed,
    # so it is what an older .env carries forward.
    if provider and provider not in _HTTP_PROVIDERS:
        extra = (
            " The SMTP transport was removed - Render blocks outbound 25/465/587,"
            " so it could not work there anyway."
            if provider == "smtp" else ""
        )
        return (
            f"EMAIL_PROVIDER is {provider!r}, which is not one of "
            f"{', '.join(_HTTP_PROVIDERS)}. No mail will be sent.{extra}"
        )

    if provider:
        return None
    if not settings.is_production:
        return None
    return (
        "No email provider is configured, so verification codes and invitations "
        "will be logged instead of sent - nobody can complete a signup. Set "
        "BREVO_API_KEY (or RESEND_API_KEY / SENDGRID_API_KEY)."
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

    if not provider:
        # Local development, or a deployment that has not been given a key. The
        # code goes to the log so signup can still be walked end to end.
        logger.info(
            "No email provider configured - would send to %s (reply-to=%s): %s",
            recipients, reply_to, subject,
        )
        return False

    if provider not in _HTTP_PROVIDERS:
        logger.error(
            "EMAIL_PROVIDER is %r, which is not one of %s; cannot send %r",
            provider, ", ".join(_HTTP_PROVIDERS), subject,
        )
        return False

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

    raise ValueError(f"expected one of {', '.join(_HTTP_PROVIDERS)}")


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

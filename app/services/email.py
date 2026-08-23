import html
import logging
from email.message import EmailMessage
from typing import Optional, Sequence

import aiosmtplib

from app.core.config import settings

logger = logging.getLogger(__name__)


async def send_email(
    *,
    to: str | Sequence[str],
    subject: str,
    html: str,
    text: str | None = None,
    reply_to: str | None = None,
    from_name: str | None = None,
) -> bool:
    """Send mail via SMTP.

    SMTP always authenticates as our mailbox, so ``From`` stays on
    ``SMTP_FROM_EMAIL``. Pass ``reply_to`` (e.g. the user's signup email) so
    the recipient can reply straight to them.
    """
    recipients = [to] if isinstance(to, str) else [addr for addr in to if addr]
    if not recipients:
        logger.warning("send_email called with no recipients: %s", subject)
        return False

    display_name = from_name or settings.SMTP_FROM_NAME
    message = EmailMessage()
    message["From"] = f"{display_name} <{settings.SMTP_FROM_EMAIL}>"
    message["To"] = ", ".join(recipients)
    message["Subject"] = subject
    if reply_to:
        message["Reply-To"] = reply_to
    message.set_content(text or "Please view this email in an HTML capable client.")
    message.add_alternative(html, subtype="html")

    if not settings.SMTP_HOST or settings.ENVIRONMENT == "test":
        logger.info(
            "SMTP disabled - would send to %s (reply-to=%s): %s",
            recipients, reply_to, subject,
        )
        return False

    try:
        await aiosmtplib.send(
            message,
            hostname=settings.SMTP_HOST,
            port=settings.SMTP_PORT,
            username=settings.SMTP_USER or None,
            password=settings.SMTP_PASSWORD or None,
            start_tls=settings.SMTP_STARTTLS,
        )
        # Logged at INFO because "did it actually go out?" is the first question
        # asked when someone says they never got it, and silence answers nothing.
        logger.info("Sent %r to %s", subject, recipients)
        return True
    except Exception:  # noqa: BLE001 - never let mail failure break a request
        logger.exception("Failed to send email to %s", recipients)
        return False


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
    return await send_email(to=to, subject=f"{code} is your WebImove code", html=_wrap(body))


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
    return await send_email(to=to, subject=f"{org} invited you to WebImove", html=_wrap(body))


async def send_client_invite_email(*, to: str, name: str, org: str, link: str,
                                   consultant: str | None = None) -> bool:
    body = f"""
    <h2 style="margin:0 0 8px">{org} created your client workspace</h2>
    <p style="color:#5b686c">Hi {name}, {"your consultant " + consultant + " at " if consultant else ""}{org}
    created your workspace. Track your request, upload documents and follow every stage
    from the WebImove app.</p>
    <a href="{link}" style="display:inline-block;margin-top:16px;background:#0f9fa8;color:#fff;
       padding:12px 20px;border-radius:8px;text-decoration:none">Get started</a>"""
    return await send_email(to=to, subject=f"{org} invited you to WebImove", html=_wrap(body))

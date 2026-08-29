"""Choosing an email provider, and the request each one gets.

The bug behind this was invisible from the code: `smtp.gmail.com:587` is a
correct configuration everywhere except the one place it was deployed. Render
drops outbound 25/465/587 and Gmail offers only 465 and 587, so every send timed
out - and because `send_email` deliberately swallows failures so a signup is not
lost, there was nothing to see. The SMTP transport has since been removed
outright rather than kept as a trap; mail goes over HTTPS.

So two things are pinned here. That the provider is picked from whichever key is
actually present, because an operator pasting one key and getting no behaviour
change is the same silent failure again. And the shape of each provider's
request, because a wrong field name comes back as a 400 that also gets
swallowed.
"""
import pytest

from app.services import email


@pytest.fixture(autouse=True)
def clean_settings(monkeypatch):
    """A configuration that sends nowhere, so each test states its own."""
    for name, value in (
        ("EMAIL_PROVIDER", ""),
        ("RESEND_API_KEY", ""),
        ("BREVO_API_KEY", ""),
        ("SENDGRID_API_KEY", ""),
        ("SMTP_HOST", "smtp.gmail.com"),
        ("SMTP_PORT", 587),
        ("SMTP_FROM_EMAIL", "no-reply@webimove.com"),
        ("SMTP_FROM_NAME", "WebImove"),
        ("ENVIRONMENT", "production"),
    ):
        monkeypatch.setattr(email.settings, name, value, raising=False)
    # Not on a PaaS unless a test says so.
    monkeypatch.setattr(type(email.settings), "is_paas",
                        property(lambda self: False))


class TestTransportChoice:
    def test_nothing_configured_means_mail_is_logged_not_sent(self, monkeypatch):
        """The local path before you have any credentials. Signup still
        completes end to end and the verification code goes to the console."""
        monkeypatch.setattr(email.settings, "SMTP_HOST", "localhost", raising=False)

        assert email.active_provider() == ""
        assert "logged" in email.describe_transport()

    def test_smtp_is_used_when_a_real_host_is_configured(self):
        """Gmail SMTP is the easiest thing to point at while developing."""
        assert email.active_provider() == "smtp"
        assert "smtp.gmail.com:587" in email.describe_transport()

    def test_an_http_key_wins_over_smtp(self, monkeypatch):
        """SMTP is auto-detected last: it is the one that cannot work on every
        host, so a configured API key is the safer default."""
        monkeypatch.setattr(email.settings, "RESEND_API_KEY", "re_x", raising=False)

        assert email.active_provider() == "resend"

    def test_smtp_can_be_forced_over_a_present_api_key(self, monkeypatch):
        """Local development against Gmail while the production keys sit in the
        same .env."""
        monkeypatch.setattr(email.settings, "RESEND_API_KEY", "re_x", raising=False)
        monkeypatch.setattr(email.settings, "EMAIL_PROVIDER", "smtp", raising=False)

        assert email.active_provider() == "smtp"

    def test_brevo_wins_when_several_keys_are_present(self, monkeypatch):
        """Brevo is the deployment default, so it is first in the search order."""
        monkeypatch.setattr(email.settings, "BREVO_API_KEY", "b", raising=False)
        monkeypatch.setattr(email.settings, "RESEND_API_KEY", "r", raising=False)
        monkeypatch.setattr(email.settings, "SENDGRID_API_KEY", "s", raising=False)

        assert email.active_provider() == "brevo"

    @pytest.mark.parametrize("key_name,expected", [
        ("RESEND_API_KEY", "resend"),
        ("BREVO_API_KEY", "brevo"),
        ("SENDGRID_API_KEY", "sendgrid"),
    ])
    def test_one_key_is_enough(self, monkeypatch, key_name, expected):
        """Pasting a key is the whole configuration.

        The alternative - a key *and* a provider setting - is two things to keep
        in sync, and the failure when they disagree is silent.
        """
        monkeypatch.setattr(email.settings, key_name, "test-key", raising=False)

        assert email.active_provider() == expected

    def test_an_explicit_provider_wins(self, monkeypatch):
        """For when two keys are present and only one is meant to be live."""
        monkeypatch.setattr(email.settings, "RESEND_API_KEY", "r", raising=False)
        monkeypatch.setattr(email.settings, "SENDGRID_API_KEY", "s", raising=False)
        monkeypatch.setattr(email.settings, "EMAIL_PROVIDER", "sendgrid", raising=False)

        assert email.active_provider() == "sendgrid"

    def test_a_key_with_whitespace_still_counts(self, monkeypatch):
        """A value pasted into a dashboard arrives with a trailing newline more
        often than not, and an untrimmed key would read as absent."""
        monkeypatch.setattr(email.settings, "BREVO_API_KEY", "  key  \n", raising=False)

        assert email.active_provider() == "brevo"


class TestUnconfiguredWarning:
    """The check that turns a silent production failure into one line at boot.

    Without it, a deployment with no key looks entirely healthy: signup returns
    201, the account exists, and the code is written to a log nobody reads.
    """

    def test_no_provider_in_production_is_reported(self, monkeypatch):
        # No API key and no real SMTP host: nothing to send with at all.
        monkeypatch.setattr(email.settings, "SMTP_HOST", "localhost", raising=False)

        warning = email.mail_is_not_configured()

        assert warning is not None
        # Actionable, not just a diagnosis.
        assert "BREVO_API_KEY" in warning

    def test_no_provider_locally_is_fine(self, monkeypatch):
        """Warning on every local boot would train the operator to ignore the
        message that matters."""
        monkeypatch.setattr(email.settings, "SMTP_HOST", "localhost", raising=False)
        monkeypatch.setattr(email.settings, "ENVIRONMENT", "development", raising=False)

        assert email.mail_is_not_configured() is None

    def test_a_configured_provider_is_fine(self, monkeypatch):
        """The fix itself must not still be reported as the problem."""
        monkeypatch.setattr(email.settings, "BREVO_API_KEY", "key", raising=False)

        assert email.mail_is_not_configured() is None
        assert "brevo" in email.describe_transport()

    def test_gmail_on_a_paas_is_reported(self, monkeypatch):
        """The easy choice being the wrong one. Render drops outbound 587 and
        Gmail offers nothing else, so this configuration cannot work there - and
        the symptom is a connect timeout that reads like bad credentials."""
        monkeypatch.setattr(email.settings, "EMAIL_PROVIDER", "smtp", raising=False)
        monkeypatch.setattr(type(email.settings), "is_paas",
                            property(lambda self: True))

        warning = email.mail_is_not_configured()

        assert warning is not None and "587" in warning
        # Actionable, not just a diagnosis.
        assert "BREVO_API_KEY" in warning

    def test_the_same_smtp_configuration_locally_is_fine(self, monkeypatch):
        """Gmail on 587 from a laptop works. Warning about it would train the
        operator to ignore the message that matters."""
        monkeypatch.setattr(email.settings, "EMAIL_PROVIDER", "smtp", raising=False)

        assert email.mail_is_not_configured() is None

    def test_an_unblocked_smtp_port_is_fine_on_a_paas(self, monkeypatch):
        """2525 is the escape hatch Brevo and SendGrid offer, and it is not
        blocked - so someone using it must not be told it will fail."""
        monkeypatch.setattr(email.settings, "EMAIL_PROVIDER", "smtp", raising=False)
        monkeypatch.setattr(email.settings, "SMTP_PORT", 2525, raising=False)
        monkeypatch.setattr(type(email.settings), "is_paas",
                            property(lambda self: True))

        assert email.mail_is_not_configured() is None

    def test_an_unrecognised_provider_is_reported_in_any_environment(self, monkeypatch):
        """Unlike a missing key, a wrong name is always a mistake - reporting it
        only in production would let it ship."""
        monkeypatch.setattr(email.settings, "ENVIRONMENT", "development", raising=False)
        monkeypatch.setattr(email.settings, "EMAIL_PROVIDER", "mailgun", raising=False)

        assert "mailgun" in (email.mail_is_not_configured() or "")


class TestProviderRequests:
    """The exact request each provider receives.

    A wrong field name is a 400 that `send_email` logs and swallows, so it looks
    the same from the outside as mail that was never attempted.
    """

    def build(self, provider):
        return email._build_request(
            provider=provider,
            key="test-key",
            recipients=["ruhul@yopmail.com"],
            subject="123456 is your WebImove code",
            html="<p>code</p>",
            text="code",
            reply_to="someone@example.com",
            display_name="WebImove",
        )

    def test_resend(self):
        url, headers, payload = self.build("resend")

        assert url == "https://api.resend.com/emails"
        assert headers["Authorization"] == "Bearer test-key"
        assert payload["from"] == "WebImove <no-reply@webimove.com>"
        assert payload["to"] == ["ruhul@yopmail.com"]
        assert payload["html"] == "<p>code</p>"
        assert payload["reply_to"] == "someone@example.com"

    def test_brevo(self):
        url, headers, payload = self.build("brevo")

        assert url == "https://api.brevo.com/v3/smtp/email"
        # Brevo authenticates on its own header, not a bearer token.
        assert headers["api-key"] == "test-key"
        assert payload["sender"] == {"email": "no-reply@webimove.com",
                                    "name": "WebImove"}
        assert payload["to"] == [{"email": "ruhul@yopmail.com"}]
        assert payload["htmlContent"] == "<p>code</p>"

    def test_sendgrid(self):
        url, headers, payload = self.build("sendgrid")

        assert url == "https://api.sendgrid.com/v3/mail/send"
        assert headers["Authorization"] == "Bearer test-key"
        assert payload["personalizations"] == [
            {"to": [{"email": "ruhul@yopmail.com"}]}
        ]
        # Order matters: SendGrid shows the last part the client can render, so
        # html after text. Reversed, every email arrives as plain text.
        assert [c["type"] for c in payload["content"]] == ["text/plain", "text/html"]

    def test_an_unknown_provider_is_rejected_by_name(self):
        """A typo in EMAIL_PROVIDER should say what the options were."""
        with pytest.raises(ValueError, match="resend"):
            self.build("mailchimp")


class TestSending:
    async def test_a_provider_send_posts_and_reports_success(self, monkeypatch):
        monkeypatch.setattr(email.settings, "RESEND_API_KEY", "key", raising=False)
        posted = {}

        class FakeResponse:
            status_code = 200
            text = "{}"

        class FakeClient:
            async def post(self, url, headers=None, json=None):
                posted.update(url=url, headers=headers, json=json)
                return FakeResponse()

        monkeypatch.setattr(email, "_http", lambda: FakeClient())

        sent = await email.send_email(
            to="ruhul@yopmail.com", subject="Code", html="<p>1</p>")

        assert sent is True
        assert posted["url"] == "https://api.resend.com/emails"

    async def test_a_refused_send_returns_false_rather_than_raising(self, monkeypatch):
        """Mail is a side effect. A signup whose code could not be sent must
        still record the account - the caller decides what to tell the user."""
        monkeypatch.setattr(email.settings, "BREVO_API_KEY", "key", raising=False)

        class FakeResponse:
            status_code = 401
            text = '{"message":"Key not found"}'

        class FakeClient:
            async def post(self, *_, **__):
                return FakeResponse()

        monkeypatch.setattr(email, "_http", lambda: FakeClient())

        assert await email.send_email(
            to="a@b.c", subject="Code", html="<p>1</p>") is False

    async def test_an_unreachable_provider_returns_false(self, monkeypatch):
        monkeypatch.setattr(email.settings, "RESEND_API_KEY", "key", raising=False)

        class FakeClient:
            async def post(self, *_, **__):
                raise email.httpx.ConnectError("no route to host")

        monkeypatch.setattr(email, "_http", lambda: FakeClient())

        assert await email.send_email(
            to="a@b.c", subject="Code", html="<p>1</p>") is False

    async def test_no_recipients_sends_nothing(self, monkeypatch):
        monkeypatch.setattr(email.settings, "RESEND_API_KEY", "key", raising=False)

        assert await email.send_email(to=[], subject="Code", html="x") is False

    async def test_the_otp_email_carries_a_plain_text_part(self, monkeypatch):
        """A code is what a lock-screen preview shows, and a preview of
        "Please view this email in an HTML capable client" is useless."""
        monkeypatch.setattr(email.settings, "RESEND_API_KEY", "key", raising=False)
        captured = {}

        class FakeResponse:
            status_code = 200
            text = "{}"

        class FakeClient:
            async def post(self, url, headers=None, json=None):
                captured.update(json)
                return FakeResponse()

        monkeypatch.setattr(email, "_http", lambda: FakeClient())

        await email.send_otp_email(to="a@b.c", code="123456",
                                   purpose="email_verification")

        assert "123456" in captured["text"]
        assert "123456" in captured["html"]

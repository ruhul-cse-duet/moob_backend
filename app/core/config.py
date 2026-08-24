from functools import lru_cache
from typing import Dict, List, Optional

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    # App
    APP_NAME: str = "WebImove API"
    ENVIRONMENT: str = "development"
    API_V1_PREFIX: str = "/api/v1"
    DEBUG: bool = True
    # This API is consumed by Flutter apps, and CORS is a browser mechanism -
    # a native HTTP client sends no Origin and performs no preflight, so the
    # list has no effect on them at all. It applies only to browser callers
    # (the consultant website, Flutter Web, /docs).
    #
    # "*" is safe *here* specifically because a wildcard also switches
    # `allow_credentials` off in main.py. Cookies are therefore never sent
    # cross-origin, and auth is a bearer token the attacking page cannot read
    # from another origin - so a hostile site gains nothing it could not
    # already do from its own server, without a browser.
    #
    # Name the real origins instead if a browser client ever needs cookie
    # authentication; credentialed CORS turns itself back on automatically.
    BACKEND_CORS_ORIGINS: List[str] = ["*"]

    # Mongo - database per tenant
    MONGODB_URI: str = "mongodb://localhost:27017"
    PLATFORM_DB_NAME: str = "webimove_platform"
    # Keep this short. Atlas caps database names at 38 bytes and the tenant id
    # that follows is a 24-character ObjectId, so the prefix has 14 to work with.
    TENANT_DB_PREFIX: str = "wm_t_"

    # Docs. Public API reference is fine while building; in production it hands
    # an attacker the whole surface, so it is off unless explicitly re-enabled.
    ENABLE_DOCS: Optional[bool] = None

    # Set to True only when the API really sits behind a proxy you control
    # (nginx, a load balancer, Cloudflare). It makes the app believe
    # X-Forwarded-For, which a direct caller can otherwise forge to dodge the
    # login throttle and to poison the audit trail.
    TRUST_PROXY_HEADERS: bool = False

    # Security response headers (HSTS is only sent over HTTPS by the browser anyway).
    SECURITY_HEADERS: bool = True
    HSTS_MAX_AGE: int = 60 * 60 * 24 * 365

    # Brute force. Counted per email+IP over a rolling window, then a lockout.
    LOGIN_MAX_ATTEMPTS: int = 8
    LOGIN_ATTEMPT_WINDOW_MINUTES: int = 15
    LOGIN_LOCKOUT_MINUTES: int = 15

    # One-time bootstrap of the first platform admin over HTTP. Empty disables
    # the endpoint entirely; the CLI script stays available either way.
    PLATFORM_SETUP_TOKEN: str = ""

    # The platform administrator to create on boot, so a fresh database is never
    # locked out of its own admin area. Both must be set or nothing is seeded.
    # Creation only: an account that already exists is left exactly as it is,
    # because a leaked .env must not be able to take over a live platform by
    # rewriting its owner's password on the next restart.
    ADMIN_EMAIL: str = ""
    ADMIN_PASSWORD: str = ""
    ADMIN_NAME: str = "Platform Admin"

    # JWT
    JWT_SECRET_KEY: str = "change-me"
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60
    REFRESH_TOKEN_EXPIRE_DAYS: int = 30

    # Frontend - invite and reset links must open the app, not this API.
    # FRONTEND_URL is where the app is actually served: in development a Flutter
    # web build picks a fresh port every run, so pin one
    # (`flutter run -d chrome --web-port 5173`) or set this to match.
    FRONTEND_URL: str = "http://localhost:5173"
    # The app routes on the URL fragment, so an invitation link without the `#`
    # opens the app's front door and drops the token on the floor.
    INVITE_ACCEPT_PATH: str = "/#/auth/invite"
    INVITE_EXPIRE_DAYS: int = 7

    # OTP / SMTP
    OTP_LENGTH: int = 6
    OTP_EXPIRE_MINUTES: int = 10
    # A full minute between codes. Short enough not to strand someone whose
    # first email went astray, long enough that the address cannot be used as a
    # free mail cannon - every resend is an email sent in someone else's name.
    OTP_RESEND_COOLDOWN_SECONDS: int = 60
    OTP_MAX_ATTEMPTS: int = 5
    SMTP_HOST: str = "localhost"
    SMTP_PORT: int = 587
    # Accept SMTP_USERNAME / SMTP_FROM as common .env aliases.
    SMTP_USER: str = Field(
        default="",
        validation_alias=AliasChoices("SMTP_USER", "SMTP_USERNAME"),
    )
    SMTP_PASSWORD: str = ""
    SMTP_FROM_EMAIL: str = Field(
        default="no-reply@webimove.com",
        validation_alias=AliasChoices("SMTP_FROM_EMAIL", "SMTP_FROM"),
    )
    SMTP_FROM_NAME: str = "WebImove"
    SMTP_STARTTLS: bool = True

    # ---- Email transport ----
    # Render blocks outbound ports 25, 465 and 587 to stop its platform being
    # used for spam, and Gmail offers SMTP on 465 and 587 and nothing else - so
    # smtp.gmail.com is unreachable from Render and every send times out. An
    # HTTP API goes over 443, which nothing blocks.
    #
    # Leave EMAIL_PROVIDER empty and the transport is picked from whichever key
    # below is filled in, falling back to SMTP when none are. So switching is one
    # key, not a key plus a mode setting to keep in sync. Name a provider here
    # only to force one when more than one key is present.
    EMAIL_PROVIDER: str = ""
    # 3,000 emails a month free. Needs a verified domain to send as your own
    # address; onboarding@resend.dev works immediately for testing.
    RESEND_API_KEY: str = ""
    # 300 a day free, no domain needed to start.
    BREVO_API_KEY: str = ""
    # 100 a day free.
    SENDGRID_API_KEY: str = ""

    # OpenAI
    OPENAI_API_KEY: str = ""
    OPENAI_MODEL: str = "gpt-4o-mini"
    OPENAI_VISION_MODEL: str = "gpt-4o-mini"
    OPENAI_MAX_TOKENS: int = 1500

    # Storage - files live in GridFS inside each tenant database, not on disk
    MAX_UPLOAD_MB: int = 25

    # Logging. LOG_LEVEL is the APPLICATION level; the others keep chatty
    # libraries quiet independently. Set MONGO_LOG_LEVEL=DEBUG only when you are
    # actually debugging the driver - it emits a heartbeat per replica node
    # every 10 seconds.
    LOG_LEVEL: str = "INFO"
    MONGO_LOG_LEVEL: str = "WARNING"
    HTTP_LOG_LEVEL: str = "WARNING"
    ACCESS_LOG: bool = True

    # Stripe — platform subscriptions, invoices, refunds
    STRIPE_SECRET_KEY: str = "sk_test_51MockupKeyHereForSafety"
    # Safe to hand to a browser: Stripe.js needs it to tokenise a card so the
    # number never reaches this server.
    STRIPE_PUBLISHABLE_KEY: str = ""
    STRIPE_WEBHOOK_SECRET: str = ""
    STRIPE_CURRENCY: str = "usd"
    # Recurring billing needs a Stripe Price per plan/cycle. Create them once in
    # the Stripe dashboard and map them here as JSON, keyed "<plan>_<cycle>":
    #   STRIPE_PRICES={"starter_monthly":"price_1A...","starter_annual":"price_1B..."}
    # Without a price the plan falls back to the legacy one-off charge, which
    # never renews.
    STRIPE_PRICES: Dict[str, str] = {}

    # ---- Push notifications (Firebase Cloud Messaging, HTTP v1) ----
    # Every notification the API writes is also pushed to the account's
    # registered devices. Credentials are a Firebase *service account*, not the
    # legacy server key - the legacy HTTP API is switched off by Google.
    #
    # Leave all of these empty and push is simply skipped: the in-app feed, the
    # socket and every REST endpoint carry on exactly as before, and
    # /notifications/devices still accepts registrations, so the tokens are
    # already there the moment credentials are filled in.
    PUSH_ENABLED: bool = True
    # Either drop the whole service-account JSON in here (or a path to it)...
    FCM_CREDENTIALS_JSON: str = ""
    # ...or name the three fields out of it individually. A private key pasted
    # into a .env keeps its newlines as a literal backslash-n; that is expected
    # and handled.
    FCM_PROJECT_ID: str = ""
    FCM_CLIENT_EMAIL: str = ""
    FCM_PRIVATE_KEY: str = ""
    # The Android notification channel the app creates. Must match the id in
    # PushService, or Android silently drops the heads-up presentation.
    PUSH_ANDROID_CHANNEL_ID: str = "webimove_default"
    PUSH_SOUND: str = "default"
    # How long FCM keeps trying a device that is offline.
    PUSH_TTL_SECONDS: int = 60 * 60 * 24 * 7
    # iOS bundle id. Only needed if the APNs topic differs from the one Firebase
    # derives from the project itself.
    APNS_BUNDLE_ID: str = ""
    # Sends everything to FCM's validate_only endpoint instead of a device -
    # proves the credentials and the payload without ringing a phone.
    PUSH_DRY_RUN: bool = False
    # A single fanout (an announcement to every account) is chunked so one
    # broadcast cannot hold the event loop or FCM's rate limit hostage.
    PUSH_MAX_CONCURRENCY: int = 10
    # Above this many recipients the per-device iOS badge count is skipped: it
    # costs one query per account and a broadcast does not need to be exact.
    PUSH_BADGE_RECIPIENT_LIMIT: int = 50

    # ---- Keeping a free-tier instance awake ----
    # Render's free plan stops a service after 15 minutes with no inbound
    # request. This pings itself just under that, which holds a running instance
    # up - it cannot wake one that has already stopped. The external pinger in
    # .github/workflows/keep-awake.yml is what does that half.
    #
    # Note the 750 instance-hours a month a free Render account gets, shared
    # across every free service on it: one service awake 24/7 is ~730 hours and
    # fits; two is 1460 and does not.
    KEEPALIVE_ENABLED: bool = True
    # Left empty on purpose. On Render the public URL is read from the
    # RENDER_EXTERNAL_URL variable Render sets itself, so there is nothing to
    # fill in; set this only when deploying somewhere that does not.
    KEEPALIVE_URL: str = ""
    # Under Render's 15-minute idle window, with room for a late tick.
    KEEPALIVE_INTERVAL_MINUTES: int = 12


    # ------------------------------------------------------------------ #
    # Derived helpers
    # ------------------------------------------------------------------ #
    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT.lower() in {"production", "prod"}

    @property
    def is_paas(self) -> bool:
        """Whether this is running on a host that blocks outbound SMTP.

        Detected from the variables the platforms set themselves rather than
        from a setting, because the whole point is to warn an operator who does
        not yet know this is the problem - and they will not have set a flag
        saying so.
        """
        import os

        return any(os.getenv(name) for name in (
            "RENDER", "RENDER_EXTERNAL_URL",  # Render
            "DYNO",                            # Heroku
            "FLY_APP_NAME",                    # Fly.io
            "RAILWAY_ENVIRONMENT",             # Railway
        ))

    @property
    def push_configured(self) -> bool:
        """Whether there is enough here to reach FCM at all."""
        if not self.PUSH_ENABLED:
            return False
        if self.FCM_CREDENTIALS_JSON.strip():
            return True
        return bool(self.FCM_PROJECT_ID and self.FCM_CLIENT_EMAIL
                    and self.FCM_PRIVATE_KEY)

    @property
    def docs_enabled(self) -> bool:
        if self.ENABLE_DOCS is not None:
            return self.ENABLE_DOCS
        return not self.is_production

    @property
    def cors_allows_any_origin(self) -> bool:
        return "*" in self.BACKEND_CORS_ORIGINS

    def insecure_settings(self) -> List[str]:
        """Configuration that must never reach production.

        Returned rather than raised so the caller decides between a hard stop
        and a loud warning - a half-configured staging box should still boot.
        """
        problems: List[str] = []
        if self.JWT_SECRET_KEY in {"change-me", "change-me-to-a-long-random-string", ""}:
            problems.append(
                "JWT_SECRET_KEY is still the placeholder - anyone can mint a valid "
                "token for any account. Set it to a long random string."
            )
        elif len(self.JWT_SECRET_KEY) < 32:
            problems.append("JWT_SECRET_KEY is shorter than 32 characters")
        if self.DEBUG:
            problems.append("DEBUG is on - error responses carry exception text")
        # A wildcard origin is deliberate on a mobile-first API and is not
        # listed here: it forces credentials off, which leaves a hostile page
        # able to make only unauthenticated calls it could make from its own
        # server anyway. main.py logs the effective policy on boot instead.
        if not self.STRIPE_WEBHOOK_SECRET:
            problems.append(
                "STRIPE_WEBHOOK_SECRET is empty - renewals and failed payments "
                "will never be recorded"
            )
        # Only in production: seeding is a development convenience, and warning
        # about it on every local boot would train the operator to ignore this
        # whole list.
        if self.ADMIN_PASSWORD and self.is_production:
            problems.append(
                "ADMIN_PASSWORD is set - the seeded administrator owns the whole "
                "platform, so clear it once the account exists"
            )
        if self.STRIPE_SECRET_KEY and not self.STRIPE_PUBLISHABLE_KEY:
            problems.append(
                "STRIPE_PUBLISHABLE_KEY is empty while a secret key is set - "
                "the client cannot tokenise a card, so signup will not take "
                "payment"
            )
        return problems


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()

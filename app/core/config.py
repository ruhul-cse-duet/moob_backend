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


    # ------------------------------------------------------------------ #
    # Derived helpers
    # ------------------------------------------------------------------ #
    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT.lower() in {"production", "prod"}

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

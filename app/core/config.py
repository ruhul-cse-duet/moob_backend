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

    # JWT
    JWT_SECRET_KEY: str = "change-me"
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60
    REFRESH_TOKEN_EXPIRE_DAYS: int = 30

    # Frontend - invite and reset links must open the app, not this API
    FRONTEND_URL: str = "http://localhost:5173"
    INVITE_ACCEPT_PATH: str = "/invite/accept"
    INVITE_EXPIRE_DAYS: int = 7

    # OTP / SMTP
    OTP_LENGTH: int = 6
    OTP_EXPIRE_MINUTES: int = 10
    OTP_RESEND_COOLDOWN_SECONDS: int = 30
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
        if self.cors_allows_any_origin:
            problems.append(
                'BACKEND_CORS_ORIGINS is ["*"] - list the real frontend origins instead'
            )
        if not self.STRIPE_WEBHOOK_SECRET:
            problems.append(
                "STRIPE_WEBHOOK_SECRET is empty - renewals and failed payments "
                "will never be recorded"
            )
        return problems


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()

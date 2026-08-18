from functools import lru_cache
from typing import Dict, List

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


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()

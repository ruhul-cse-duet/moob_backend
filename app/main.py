import logging
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from app.api import api_router
from app.core.config import settings
from app.core.errors import baseline_headers, register_exception_handlers
from app.core.logging import setup_logging
from app.db.indexes import ensure_platform_indexes
from app.db.mongo import close, connect
from app.db.seed import seed_platform_admin, seed_policies

setup_logging()
logger = logging.getLogger("app.main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        connect()
        await ensure_platform_indexes()
        logger.info("Database connected; indexes ready")
    except Exception as exc:  # noqa: BLE001 - boot anyway; /health reports the truth
        # One line, not a 30-line stack trace. The full traceback is available at DEBUG.
        logger.error(
            "Database not reachable at startup - running in degraded mode (%s: %s). "
            "Check MONGODB_URI and your Atlas IP allowlist.",
            type(exc).__name__, str(exc).split(",")[0],
        )
        logger.debug("Startup database error", exc_info=True)
    else:
        # After the indexes, never before: platform_admins.email is unique, and
        # the seeder leans on that when two workers boot at the same moment.
        # Its own try, so a seeding failure is never reported as an unreachable
        # database - and never stops an otherwise healthy API from serving.
        try:
            await seed_platform_admin()
            # Terms and privacy are shown to people who have no account yet, so
            # they cannot wait for an administrator to get around to writing
            # them. Seeded once; an administrator's own version replaces them.
            await seed_policies()
        except Exception as exc:  # noqa: BLE001
            logger.error("Could not seed the platform defaults: %s", exc)
            logger.debug("Seed error", exc_info=True)
    for problem in settings.insecure_settings():
        # Loud, not fatal: refusing to boot would take a running platform down
        # over a setting that may be deliberate. One line per problem, at the
        # level the operator is most likely to be watching.
        if settings.is_production:
            logger.error("INSECURE CONFIGURATION: %s", problem)
        else:
            logger.warning("Configuration warning: %s", problem)
    logger.info("%s started (%s)", settings.APP_NAME, settings.ENVIRONMENT)
    yield
    await close()


app = FastAPI(
    title=settings.APP_NAME,
    version="1.0.0",
    description=(
        "WebImove - AI powered immigration case management.\n\n"
        "One API serves the Consultant, Partner and Client mobile apps **and** the "
        "consultant website. Access is scoped by the role inside the JWT, not by a "
        "separate API per surface.\n\n"
        "Tenancy: **database per organization**. The platform database holds the tenant "
        "registry, the global email directory, OTP codes and subscriptions; every "
        "organization gets its own Mongo database for cases, documents and people.\n\n"
        "**Errors** always return "
        "`{success, message, detail, code, errors, status_code}`."
    ),
    lifespan=lifespan,
    docs_url="/docs" if settings.docs_enabled else None,
    redoc_url="/redoc" if settings.docs_enabled else None,
    openapi_url=(
        f"{settings.API_V1_PREFIX}/openapi.json" if settings.docs_enabled else None
    ),
)


@app.middleware("http")
async def request_context(request: Request, call_next):
    """One id per request, echoed on the response and into every error body.

    A user can quote it in a support ticket and we can find the exact log line -
    otherwise a 500 is untraceable across tenants.
    """
    request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex
    request.state.request_id = request_id
    response = await call_next(request)
    for header, value in baseline_headers(request_id).items():
        response.headers.setdefault(header, value)
    return response


# `allow_credentials` with a wildcard origin is rejected by every browser and
# would silently break the cookie-less bearer flow the apps rely on. Send
# credentials only when the operator has named the origins.
_cors_any_origin = settings.cors_allows_any_origin
if _cors_any_origin and settings.is_production:
    logger.error(
        "CORS is open to every origin in production. Set BACKEND_CORS_ORIGINS "
        "to the real frontend origins."
    )

# A Flutter web debug server picks a fresh port on every run, so no fixed list
# can name it and a developer would meet "Disallowed CORS origin" instead of
# their app. Outside production any loopback origin is accepted; production is
# held to BACKEND_CORS_ORIGINS alone.
_cors_origin_regex = (
    None if settings.is_production
    else r"http://(localhost|127\.0\.0\.1)(:\d+)?"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.BACKEND_CORS_ORIGINS,
    allow_origin_regex=_cors_origin_regex,
    allow_credentials=not _cors_any_origin,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Request-ID", "Content-Disposition"],
)

register_exception_handlers(app)

app.include_router(api_router, prefix=settings.API_V1_PREFIX)


@app.get("/", tags=["Health"])
async def root():
    return {
        "success": True,
        "message": "WebImove API is running",
        "name": settings.APP_NAME,
        "status": "ok",
        "docs": "/docs",
    }


@app.get("/health", tags=["Health"])
async def health():
    from app.db.mongo import get_client
    try:
        await get_client().admin.command("ping")
        db_ok = True
    except Exception:  # noqa: BLE001
        db_ok = False
    return {
        "success": db_ok,
        "message": "Healthy" if db_ok else "Database unreachable",
        "status": "ok" if db_ok else "degraded",
        "database": db_ok,
        "environment": settings.ENVIRONMENT,
    }

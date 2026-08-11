import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import api_router
from app.core.config import settings
from app.core.errors import register_exception_handlers
from app.core.logging import setup_logging
from app.db.indexes import ensure_platform_indexes
from app.db.mongo import close, connect

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
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url=f"{settings.API_V1_PREFIX}/openapi.json",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.BACKEND_CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
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

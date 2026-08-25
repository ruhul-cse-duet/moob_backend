"""
Logging setup.

The mistake this replaces: setting the ROOT logger to DEBUG, which every third-party
library inherits. PyMongo then logs its topology monitoring — three streaming heartbeats
every ten seconds, forever — and buries the application's own output.

Application verbosity and library verbosity are separate knobs here.
"""
import logging
import sys

from app.core.config import settings

# Libraries that are useful at WARNING and unreadable below it.
NOISY = {
    "pymongo": "MONGO_LOG_LEVEL",
    "pymongo.topology": "MONGO_LOG_LEVEL",
    "pymongo.serverSelection": "MONGO_LOG_LEVEL",
    "pymongo.connection": "MONGO_LOG_LEVEL",
    "pymongo.command": "MONGO_LOG_LEVEL",
    "pymongo.heartbeat": "MONGO_LOG_LEVEL",
    "motor": "MONGO_LOG_LEVEL",
    "httpx": "HTTP_LOG_LEVEL",
    "httpcore": "HTTP_LOG_LEVEL",
    "anthropic": "HTTP_LOG_LEVEL",
    "urllib3": "HTTP_LOG_LEVEL",
    "asyncio": "HTTP_LOG_LEVEL",
    "multipart": "HTTP_LOG_LEVEL",
    "passlib": "HTTP_LOG_LEVEL",
}


def setup_logging() -> None:
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s  %(levelname)-8s %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    ))
    root.addHandler(handler)
    root.setLevel(logging.WARNING)          # libraries default to quiet

    # The application's own loggers follow LOG_LEVEL.
    logging.getLogger("app").setLevel(settings.LOG_LEVEL.upper())

    for name, setting in NOISY.items():
        logging.getLogger(name).setLevel(getattr(settings, setting).upper())

    logging.getLogger("uvicorn").setLevel(settings.LOG_LEVEL.upper())
    logging.getLogger("uvicorn.error").setLevel(settings.LOG_LEVEL.upper())
    logging.getLogger("uvicorn.access").setLevel(
        "INFO" if settings.ACCESS_LOG else "WARNING")

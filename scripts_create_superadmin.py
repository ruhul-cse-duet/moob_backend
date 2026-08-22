"""Create the platform super admin.

    python scripts_create_superadmin.py                      # uses ADMIN_EMAIL / ADMIN_PASSWORD
    python scripts_create_superadmin.py <email> <password> [name]

With no arguments this does exactly what booting the API does, which is the
point: one code path, so the script can never drift from the seeder and create
an account the app would not recognise.
"""
import asyncio
import logging
import sys

from app.core.config import settings
from app.db.indexes import ensure_platform_indexes
from app.db.mongo import close, connect
from app.db.seed import seed_platform_admin


async def main(email: str = "", password: str = "", name: str = "") -> None:
    connect()
    try:
        await ensure_platform_indexes()
        created = await seed_platform_admin(
            email=email or None, password=password or None, name=name or None,
        )
        if not created and not (email or settings.ADMIN_EMAIL):
            print("Set ADMIN_EMAIL and ADMIN_PASSWORD, or pass them as arguments.")
    finally:
        await close()


if __name__ == "__main__":
    # The seeder says what happened through the log, so make sure it is visible
    # when run as a one-off script rather than under the API's logging setup.
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    asyncio.run(main(*sys.argv[1:4]))

"""Create the platform super admin:  python scripts_create_superadmin.py <email> <password>"""
import asyncio
import sys

from app.core.enums import UserStatus
from app.core.security import hash_password
from app.core.utils import utcnow
from app.db.indexes import ensure_platform_indexes
from app.db.mongo import platform_db


async def main(email: str, password: str, name: str = "Platform Admin") -> None:
    await ensure_platform_indexes()
    db = platform_db()
    if await db.platform_admins.find_one({"email": email.lower()}):
        print("Super admin already exists")
        return
    await db.platform_admins.insert_one({
        "email": email.lower(),
        "full_name": name,
        "password_hash": hash_password(password),
        "status": UserStatus.ACTIVE.value,
        "created_at": utcnow(),
    })
    print(f"Super admin created: {email}")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        raise SystemExit("usage: python scripts_create_superadmin.py <email> <password>")
    asyncio.run(main(sys.argv[1], sys.argv[2], *sys.argv[3:]))

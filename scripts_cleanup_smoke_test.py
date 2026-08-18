"""Remove the smoke-test tenants created while testing the API.

Deletes only records whose organization name starts with "Smoke" and the
matching <prefix><6 digits>@yopmail.com accounts. The platform super admin and
the two pre-existing audit rows are left alone.

    python scripts_cleanup_smoke_test.py --dry-run
    python scripts_cleanup_smoke_test.py
"""
import datetime
import sys

from bson import ObjectId
from pymongo import MongoClient

from app.core.config import settings

DRY = "--dry-run" in sys.argv
EMAIL = {"$regex": r"^(c|cl|p|cons|partner|client)[0-9]{6}@yopmail\.com$"}
# Everything below was created on the smoke-test day; earlier audit rows stay.
CUTOFF = datetime.datetime(2026, 8, 18)


def main() -> None:
    mc = MongoClient(settings.MONGODB_URI)
    p = mc[settings.PLATFORM_DB_NAME]

    ids = [str(d["_id"]) for d in p.tenants.find({"name": {"$regex": "^Smoke"}}, {"_id": 1})]
    print(f"smoke-test tenants found: {len(ids)}")
    if not ids:
        print("nothing to clean")
        return

    dbs = mc.list_database_names()
    for tid in ids:
        for prefix in ("wm_t_", "webimove_tenant_"):
            name = prefix + tid
            if name in dbs:
                print(f"  drop database {name}")
                if not DRY:
                    mc.drop_database(name)

    plan = [
        ("tenants", p.tenants, {"_id": {"$in": [ObjectId(i) for i in ids]}}),
        ("signups", p.signups, {"email": EMAIL}),
        ("user_directory", p.user_directory, {"email": EMAIL}),
        ("otp_codes", p.otp_codes, {"email": EMAIL}),
        ("subscriptions", p.subscriptions, {"tenant_id": {"$in": ids}}),
        ("refresh_tokens", p.refresh_tokens, {"$or": [{"tenant_id": {"$in": ids}},
                                                      {"tenant_id": None}]}),
        ("audit_log", p.audit_log, {"created_at": {"$gte": CUTOFF}}),
    ]
    for label, col, query in plan:
        n = col.count_documents(query)
        print(f"  {label}: {n}")
        if not DRY and n:
            col.delete_many(query)

    print()
    print("dry run - nothing deleted" if DRY else "cleanup complete")
    print(f"remaining tenants: {p.tenants.count_documents({})} | "
          f"platform admins: {p.platform_admins.count_documents({})} | "
          f"audit rows: {p.audit_log.count_documents({})}")


if __name__ == "__main__":
    main()

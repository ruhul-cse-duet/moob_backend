"""
Backfill consultant ownership onto records created before ownership existed.

    python scripts_backfill_consultant_id.py            # every active tenant
    python scripts_backfill_consultant_id.py <tenant_id>
    python scripts_backfill_consultant_id.py --dry-run

Safe to re-run: it only touches records where the field is missing or null.
"""
import asyncio
import sys

from app.core.enums import Role, TenantStatus
from app.core.utils import utcnow
from app.db.indexes import ensure_tenant_indexes
from app.db.mongo import platform_db, tenant_db

MISSING = {"$in": [None, ""]}


async def backfill_tenant(tenant_id: str, name: str, dry_run: bool) -> dict:
    db = tenant_db(tenant_id)
    owner = await db.users.find_one({"role": Role.CONSULTANT_OWNER.value}, {"_id": 1})
    if not owner:
        return {"tenant": name, "skipped": "no owner account"}
    owner_id = str(owner["_id"])
    now = utcnow()
    report = {"tenant": name, "tenant_id": tenant_id, "changes": {}}

    async def fix(coll: str, filt: dict, value_from=None):
        query = {"$or": [{"consultant_id": MISSING},
                         {"consultant_id": {"$exists": False}}], **filt}
        count = await db[coll].count_documents(query)
        if count and not dry_run:
            await db[coll].update_many(
                query, {"$set": {"consultant_id": value_from or owner_id,
                                 "updated_at": now}})
        if count:
            report["changes"][coll] = count

    # Clients inherit the owner unless already assigned.
    await fix("users", {"role": Role.CLIENT.value})
    await fix("requests", {})
    await fix("cases", {})
    await fix("appointments", {})

    # Children inherit from their parent record, falling back to the owner.
    for coll, parent, parent_coll in (("documents", "case_id", "cases"),
                                      ("documents", "request_id", "requests"),
                                      ("tasks", "case_id", "cases"),
                                      ("invoices", "case_id", "cases"),
                                      ("earnings", "case_id", "cases")):
        cursor = db[coll].find({"$or": [{"consultant_id": MISSING},
                                        {"consultant_id": {"$exists": False}}],
                                parent: {"$nin": [None, ""]}})
        n = 0
        async for row in cursor:
            from app.core.utils import oid
            try:
                p = await db[parent_coll].find_one({"_id": oid(row[parent])},
                                                   {"consultant_id": 1})
            except Exception:  # noqa: BLE001
                p = None
            cid = (p or {}).get("consultant_id") or owner_id
            if not dry_run:
                await db[coll].update_one({"_id": row["_id"]},
                                          {"$set": {"consultant_id": cid,
                                                    "updated_at": now}})
            n += 1
        if n:
            report["changes"][f"{coll} (via {parent})"] = n

    # Anything still unowned falls back to the workspace owner.
    for coll in ("documents", "tasks", "invoices", "earnings", "payouts"):
        await fix(coll, {})

    # Partners: seed consultant_ids from invited_by, then from delegated tasks.
    n = 0
    async for p in db.users.find({"role": Role.PARTNER.value}):
        pid = p["_id"]
        seed = set(p.get("consultant_ids") or [])
        if p.get("invited_by"):
            seed.add(p["invited_by"])
        for cid in await db.tasks.distinct("consultant_id",
                                           {"assignee_id": str(pid)}):
            if cid:
                seed.add(cid)
        if not seed:
            seed = {owner_id}
        if set(p.get("consultant_ids") or []) != seed:
            if not dry_run:
                await db.users.update_one({"_id": pid},
                                          {"$set": {"consultant_ids": sorted(seed),
                                                    "updated_at": now}})
            n += 1
    if n:
        report["changes"]["partners (consultant_ids)"] = n

    if not dry_run:
        await ensure_tenant_indexes(db)
    return report


async def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    dry_run = "--dry-run" in sys.argv
    db = platform_db()

    if args:
        tenants = []
        from app.core.utils import oid
        t = await db.tenants.find_one({"_id": oid(args[0])})
        if t:
            tenants.append(t)
    else:
        tenants = [t async for t in db.tenants.find(
            {"status": {"$ne": TenantStatus.CANCELLED.value}})]

    if not tenants:
        print("No tenants found.")
        return

    print(f"{'DRY RUN - ' if dry_run else ''}{len(tenants)} tenant(s)\n")
    for t in tenants:
        report = await backfill_tenant(str(t["_id"]), t["name"], dry_run)
        print(f"  {report['tenant']}")
        if report.get("skipped"):
            print(f"    skipped: {report['skipped']}")
        elif not report["changes"]:
            print("    already consistent")
        else:
            for coll, n in report["changes"].items():
                print(f"    {coll:32} {n}")
    print("\nDone." if not dry_run else "\nDry run - nothing was written.")


if __name__ == "__main__":
    asyncio.run(main())
